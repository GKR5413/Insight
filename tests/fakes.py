"""Offline test doubles: a canned EDGAR (real client logic, fake HTTP), a scripted summarizer, services."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from briefing.cache import Cache
from briefing.config import Settings
from briefing.embeddings import Embedder, HashingEmbeddings
from briefing.facets import Retriever
from briefing.llm import DraftBrief, DraftCitation, DraftClaim
from briefing.models import Quote
from briefing.services import Services
from briefing.sources.edgar import EdgarClient
from briefing.sources.fsds import FsdsStore, connect
from briefing.sources.quotes import QuoteError, QuoteService
from briefing.store import VectorStore

CIK = "1234567"
ACC_K, ACC_Q, ACC_8 = "0001234567-25-000010", "0001234567-25-000020", "0001234567-26-000030"
PARA = (
    "Acme Corp designs, manufactures and sells widgets and related services to customers around the world, "
    "and the Company expects demand for widgets to remain resilient through the next fiscal year. "
)


def _body(topic: str, n: int = 3) -> str:
    return "".join(f"<div>{topic} {PARA}</div>" for _ in range(n))


def tenk_html() -> str:
    return f"""<html><head><title>x</title></head><body>
    <ix:header><div>HIDDEN XBRL CONTEXT SHOULD NEVER APPEAR</div></ix:header>
    <div>Table of Contents</div>
    <table>
      <tr><td><div>Item 1.</div></td><td><div>Business</div></td><td>1</td></tr>
      <tr><td><div>Item 1A.</div></td><td><div>Risk Factors</div></td><td>5</td></tr>
      <tr><td><div>Item 7.</div></td><td><div>Management’s Discussion and Analysis</div></td><td>20</td></tr>
    </table>
    <div>Part I</div>
    <div>Item 1.</div><div>Business</div>
    {_body("Business overview.")}
    <div>Item 1A.</div><div>Risk Factors</div>
    {_body("Supply chain risk.")}
    <div>Part II</div>
    <div>Item 7.</div><div>Management’s Discussion and Analysis of Financial Condition and Results of Operations</div>
    {_body("Results of operations.")}
    <div>See Item 1A of this Form 10-K for more discussion of risks that could affect results.</div>
    <table>
      <tr><td></td><td>2025</td><td>2024</td></tr>
      <tr><td>Total net sales</td><td>$</td><td>1,200</td><td>$</td><td>1,000</td></tr>
      <tr><td>Net income</td><td>$</td><td>300</td><td>$</td><td>250</td></tr>
    </table>
    <div>SIGNATURE</div><div>Pursuant to the requirements of Section 13 ... signed on behalf of the registrant.</div>
    </body></html>"""


def tenq_html() -> str:
    return f"""<html><body>
    <div>Part I</div><div>Financial Information</div>
    <div>Item 1.</div><div>Financial Statements</div>
    <div>Item 2.</div><div>Management’s Discussion and Analysis of Financial Condition and Results of Operations</div>
    {_body("Quarterly results.")}
    <div>Part II</div><div>Other Information</div>
    <div>Item 1A.</div><div>Risk Factors</div>
    {_body("Quarterly risk update.")}
    </body></html>"""


def eightk_html() -> str:
    return f"""<html><body>
    <div>Item 2.02    Results of Operations and Financial Condition.</div>
    {_body("Press release issued.")}
    <div>Item 9.01    Financial Statements and Exhibits.</div>
    <div>(d) Exhibits. 99.1 Press release.</div>
    <div>SIGNATURE</div>
    </body></html>"""


def exhibit_html() -> str:
    return f"<html><body><div>Acme reports quarterly results</div>{_body('Earnings release.', 4)}</body></html>"


def r_page(title: str, rows: list[tuple[str, str, str]]) -> str:
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows)
    return (
        f'<html><body><table class="report"><tr><th rowspan="2">{title} - USD ($) $ in Millions</th><th colspan="2">12 Months Ended</th></tr>'
        f"<tr><th>Sep. 27, 2025</th><th>Sep. 28, 2024</th></tr>{body}</table></body></html>"
    )


OPERATIONS = r_page("CONSOLIDATED STATEMENTS OF OPERATIONS", [("Net sales", "$ 1,200", "$ 1,000"), ("Cost of sales", "700", "600"), ("Net income", "300", "250")])
BALANCE = r_page("CONSOLIDATED BALANCE SHEETS", [("Cash and cash equivalents", "$ 450", "$ 380"), ("Total assets", "5,000", "4,700")])


def filing_summary() -> str:
    reports = "".join(
        f"<Report><ShortName>{n}</ShortName><HtmlFileName>{f}</HtmlFileName><MenuCategory>Statements</MenuCategory></Report>"
        for n, f in (("CONSOLIDATED STATEMENTS OF OPERATIONS", "R2.htm"), ("CONSOLIDATED BALANCE SHEETS", "R4.htm"), ("CONSOLIDATED BALANCE SHEETS (Parenthetical)", "R5.htm"))
    )
    return f"<FilingSummary><MyReports>{reports}</MyReports></FilingSummary>"


class FakeEdgar(EdgarClient):
    """Real EdgarClient logic; only the network is replaced by an in-memory site."""

    def __init__(self, cache: Cache, cache_dir: Path, *, lookback_days_8k: int = 120):
        super().__init__("Tests tests@example.com", cache, cache_dir, min_interval=0, lookback_days_8k=lookback_days_8k, max_8k=4)
        self.requests: list[str] = []
        recent_8k = (date.today() - timedelta(days=10)).isoformat()
        self.site: dict[str, bytes | str] = {
            "https://www.sec.gov/files/company_tickers.json": json.dumps({"0": {"cik_str": int(CIK), "ticker": "ACME", "title": "Acme Corp"}}),
            f"https://data.sec.gov/submissions/CIK{CIK.zfill(10)}.json": json.dumps(
                {
                    "filings": {
                        "recent": {
                            "form": ["8-K", "10-Q", "10-K", "10-K"],
                            "accessionNumber": [ACC_8, ACC_Q, ACC_K, "0001234567-24-000005"],
                            "filingDate": [recent_8k, "2025-08-01", "2025-02-01", "2024-02-01"],
                            "reportDate": ["", "2025-06-30", "2024-12-31", "2023-12-31"],
                            "primaryDocument": ["acme-8k.htm", "acme-10q.htm", "acme-10k.htm", "old-10k.htm"],
                            "items": ["2.02,9.01", "", "", ""],
                        }
                    }
                }
            ),
        }
        for acc, doc, html in ((ACC_K, "acme-10k.htm", tenk_html()), (ACC_Q, "acme-10q.htm", tenq_html()), (ACC_8, "acme-8k.htm", eightk_html())):
            base = f"https://www.sec.gov/Archives/edgar/data/{CIK}/{acc.replace('-', '')}"
            self.site[f"{base}/{doc}"] = html
            if doc != "acme-8k.htm":
                self.site[f"{base}/FilingSummary.xml"] = filing_summary()
                self.site[f"{base}/R2.htm"] = OPERATIONS
                self.site[f"{base}/R4.htm"] = BALANCE
            else:
                self.site[f"{base}/index.json"] = json.dumps({"directory": {"item": [{"name": "acme-8k.htm"}, {"name": "acme-ex991.htm"}, {"name": "logo.jpg"}]}})
                self.site[f"{base}/acme-ex991.htm"] = exhibit_html()

    def _get(self, url: str) -> bytes:
        self.requests.append(url)
        if url not in self.site:
            raise FileNotFoundError(url)
        v = self.site[url]
        return v.encode() if isinstance(v, str) else v


class FakeQuotes:
    name = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def get(self, symbol: str) -> Quote:
        self.calls += 1
        if self.fail:
            raise QuoteError("provider down")
        return Quote(symbol=symbol, price=123.45, change=1.0, change_pct=0.8, source="fake")


class ScriptedSummarizer:
    """Draft/repair behaviour is supplied by the test; counts calls."""

    def __init__(self, draft_fn, repair_fn=None, name="scripted"):
        self.name = name
        self.draft_fn, self.repair_fn = draft_fn, repair_fn
        self.draft_calls = self.repair_calls = 0

    def draft(self, company, registry, by_facet, config=None):
        self.draft_calls += 1
        return DraftBrief(claims=self.draft_fn(registry, by_facet))

    def repair(self, company, registry, by_facet, failures, config=None):
        self.repair_calls += 1
        if self.repair_fn is None:
            return DraftBrief(claims=[])
        return DraftBrief(claims=self.repair_fn(registry, by_facet, failures))


def first_source(registry: dict, kind: str) -> tuple[str, dict]:
    return next((a, e) for a, e in registry.items() if e["source_type"] == kind and not e["text"].startswith("[Table]"))


def doc_claim(registry: dict, facet: str = "business", text: str = "Acme sells widgets.") -> DraftClaim:
    alias, e = first_source(registry, "doc")
    return DraftClaim(facet=facet, text=text, citations=[DraftCitation(source_id=alias, quote=e["text"][:70])])


def sheet_claim(registry: dict, facet: str = "financials") -> DraftClaim:
    alias, e = first_source(registry, "sheet")
    val = next(iter(json.loads(e["cells"]).values()))
    return DraftClaim(facet=facet, text="A reported figure.", citations=[DraftCitation(source_id=alias, quote=val)])


def bad_claim(registry: dict, facet: str = "risks") -> DraftClaim:
    alias, _ = first_source(registry, "doc")
    return DraftClaim(
        facet=facet, text="An invented statement.",
        citations=[DraftCitation(source_id=alias, quote="this sentence was never written in any filing at all")],
    )


def make_fsds(tmp_path: Path, *, accessions: tuple[str, ...] = (ACC_K,), empty: tuple[str, ...] = ()) -> FsdsStore:
    """A tiny FSDS database. `accessions` get statement lines; `empty` are filings with no usable lines."""
    path = tmp_path / "fsds.sqlite"
    conn = connect(path)
    for adsh in (*accessions, *empty):
        form = "10-K" if adsh == ACC_K else "10-Q"
        conn.execute("INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (adsh, CIK, "ACME CORP", "3571", form, "2025-12-31", "2025", "FY", "1231", "2026-02-27", "2026-02-27 16:30:00", 0, "", "x.xml", "2026q1"))
    for adsh in accessions:
        lines = [(adsh, 2, 3, "IS", 0, "H", "Revenues", "us-gaap/2025", "Net sales", 0), (adsh, 2, 4, "IS", 0, "H", "NetIncomeLoss", "us-gaap/2025", "Net income", 1),
                 (adsh, 2, 5, "IS", 0, "H", "EarningsPerShareDiluted", "us-gaap/2025", "Diluted (in dollars per share)", 0),
                 (adsh, 4, 3, "BS", 0, "H", "CashAndCashEquivalentsAtCarryingValue", "us-gaap/2025", "Cash and cash equivalents", 0)]
        facts = [(adsh, "Revenues", "us-gaap/2025", "2025-12-31", 4, "USD", "", "", 1_200_000_000.0, ""), (adsh, "Revenues", "us-gaap/2025", "2024-12-31", 4, "USD", "", "", 1_000_000_000.0, ""),
                 (adsh, "Revenues", "us-gaap/2025", "2025-12-31", 4, "USD", "ProductAxis=A;", "", 600_000_000.0, ""),
                 (adsh, "NetIncomeLoss", "us-gaap/2025", "2025-12-31", 4, "USD", "", "", 300_500_000.0, ""),
                 (adsh, "EarningsPerShareDiluted", "us-gaap/2025", "2025-12-31", 4, "USD/shares", "", "", 2.0213, ""),
                 (adsh, "CashAndCashEquivalentsAtCarryingValue", "us-gaap/2025", "2025-12-31", 0, "USD", "", "", 450_000_000.0, "")]
        conn.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?)", lines)
        conn.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?)", facts)
    conn.commit()
    conn.close()
    return FsdsStore(path)


def make_services(tmp_path: Path, summarizer, *, quotes: FakeQuotes | None = None, embedder: Embedder | None = None, fsds: FsdsStore | None = None) -> Services:
    settings = Settings(data_dir=tmp_path, llm_provider="offline", embeddings_provider="fake", embedding_dim=128, redis_url="")
    cache = Cache(None)
    emb = embedder or Embedder(HashingEmbeddings(128), "hashing", 128)
    store = VectorStore(str(tmp_path / "milvus.db"), emb.model, emb.dim)
    q = quotes or FakeQuotes()
    return Services(
        settings=settings,
        cache=cache,
        edgar=FakeEdgar(cache, tmp_path / "edgar"),
        quotes=QuoteService([q], cache, ttl=0),
        embedder=emb,
        store=store,
        retriever=Retriever(store, emb),
        summarizer=summarizer,
        fsds=fsds,
    )
