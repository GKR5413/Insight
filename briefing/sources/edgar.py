"""SEC EDGAR client: ticker resolution, filing discovery, and document fetching.

EDGAR is free and keyless but enforces fair access: a descriptive User-Agent with a contact
and at most ~10 requests/second. Filings are immutable once published, so documents are cached
on disk forever and only the (mutable) submissions index goes through the TTL cache.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from ..cache import Cache
from ..models import Company, Filing

# The three statements advisors reason from. Comprehensive income, equity and parentheticals are noise here,
# and these match the IS/BS/CF set taken from the SEC data sets.
PRIMARY_STATEMENT = re.compile(r"operations|income|earnings|balance sheet|financial (position|condition)|cash flows?", re.I)
NOT_PRIMARY = re.compile(r"comprehensive|parenthetical|equity|deficit|stockholders|shareholders", re.I)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"


class EdgarError(RuntimeError):
    pass


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        cache: Cache,
        cache_dir: Path,
        *,
        min_interval: float = 0.12,
        lookback_days_8k: int = 120,
        max_8k: int = 4,
        http: httpx.Client | None = None,
    ):
        if not user_agent.strip() or "@" not in user_agent:
            raise EdgarError(
                "SEC_USER_AGENT is not set. EDGAR requires a descriptive User-Agent that includes "
                "a contact email, e.g. SEC_USER_AGENT='Acme Wealth ops@acme.com'. Put it in .env."
            )
        self.cache = cache
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.lookback_days_8k = lookback_days_8k
        self.max_8k = max_8k
        self._http = http or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(45.0, connect=10.0),
            follow_redirects=True,
        )
        self._lock = threading.Lock()
        self._last = 0.0
        self.requests_made = 0  # network requests actually sent to SEC (cache hits excluded)

    # ------------------------------------------------------------------ http plumbing
    def _get(self, url: str) -> bytes:
        last_exc: Exception | None = None
        for attempt in range(4):
            with self._lock:  # global limiter: serialises request *starts*
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
                self.requests_made += 1
            try:
                resp = self._http.get(url)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(0.6 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_exc = EdgarError(f"HTTP {resp.status_code} from {url}")
                time.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code == 403:
                raise EdgarError(f"SEC rejected the request (403). Check SEC_USER_AGENT. URL: {url}")
            if resp.status_code == 404:
                raise FileNotFoundError(url)
            resp.raise_for_status()
            return resp.content
        raise EdgarError(f"EDGAR request failed after retries: {url} ({last_exc})")

    def fetch_bytes(self, url: str) -> bytes:
        """Immutable documents: disk-cached forever."""
        path = self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + Path(url).suffix[:8])
        if path.exists():
            return path.read_bytes()
        data = self._get(url)
        path.write_bytes(data)
        return data

    def fetch_text(self, url: str) -> str:
        return self.fetch_bytes(url).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ discovery
    def resolve(self, ticker: str) -> Company:
        symbol = ticker.strip().upper().replace(".", "-")
        if not re.fullmatch(r"[A-Z0-9\-]{1,10}", symbol):
            raise EdgarError(f"'{ticker}' is not a valid ticker symbol.")
        path = self.cache_dir / "company_tickers.json"
        if not path.exists() or time.time() - path.stat().st_mtime > 24 * 3600:
            path.write_bytes(self._get(TICKERS_URL))
        data = json.loads(path.read_text())
        for row in data.values():
            if row["ticker"].upper() == symbol:
                return Company(ticker=symbol, cik=str(row["cik_str"]), name=row["title"])
        raise EdgarError(f"No SEC registrant found for ticker '{symbol}'. (ETFs and foreign issuers are out of scope.)")

    def recent_filings(self, cik: str) -> list[Filing]:
        key = f"edgar:submissions:{cik}"
        data = self.cache.get_json(key)
        if data is None:
            data = json.loads(self._get(SUBMISSIONS_URL.format(cik10=str(cik).zfill(10))))
            self.cache.set_json(key, data, ttl=3600)
        r = data["filings"]["recent"]
        out: list[Filing] = []
        for i, form in enumerate(r["form"]):
            if form not in ("10-K", "10-Q", "8-K"):
                continue
            items = tuple(x.strip() for x in (r.get("items") or [""] * len(r["form"]))[i].split(",") if x.strip())
            out.append(
                Filing(
                    cik=str(cik),
                    accession=r["accessionNumber"][i],
                    form=form,
                    filed=r["filingDate"][i],
                    report_date=r["reportDate"][i] or "",
                    primary_doc=r["primaryDocument"][i],
                    items=items,
                )
            )
        return out

    def select_filings(self, filings: list[Filing], today: date | None = None) -> list[Filing]:
        """Latest 10-K, latest 10-Q, and recent 8-Ks (newest first)."""
        today = today or date.today()
        cutoff = today - timedelta(days=self.lookback_days_8k)
        by_date = sorted(filings, key=lambda f: f.filed, reverse=True)
        picked: list[Filing] = []
        for form in ("10-K", "10-Q"):
            first = next((f for f in by_date if f.form == form), None)
            if first:
                picked.append(first)
        eights = [f for f in by_date if f.form == "8-K" and datetime.strptime(f.filed, "%Y-%m-%d").date() >= cutoff]
        picked.extend(eights[: self.max_8k])
        return sorted(picked, key=lambda f: f.filed, reverse=True)

    # ------------------------------------------------------------------ filing contents
    def filing_files(self, filing: Filing) -> list[str]:
        try:
            data = json.loads(self.fetch_text(f"{filing.base_url}/index.json"))
        except FileNotFoundError:
            return []
        return [i["name"] for i in data.get("directory", {}).get("item", [])]

    def statement_reports(self, filing: Filing, limit: int = 4) -> list[tuple[str, str]]:
        """(short name, R-file) for the primary financial statements (R2.htm, R4.htm, ...)."""
        try:
            xml = self.fetch_text(f"{filing.base_url}/FilingSummary.xml")
        except FileNotFoundError:
            return []
        soup = BeautifulSoup(xml, "lxml-xml")
        out: list[tuple[str, str]] = []
        for rep in soup.find_all("Report"):
            cat = rep.find("MenuCategory")
            name = rep.find("ShortName")
            html = rep.find("HtmlFileName")
            if not (cat and name and html) or cat.text.strip() != "Statements":
                continue
            short = name.text.strip()
            if not html.text.strip().lower().endswith((".htm", ".html")) or not PRIMARY_STATEMENT.search(short) or NOT_PRIMARY.search(short):
                continue
            out.append((short, html.text.strip()))
        return out[:limit]

    def exhibit_99_files(self, filing: Filing, limit: int = 2) -> list[str]:
        names = [n for n in self.filing_files(filing) if re.search(r"ex[-_]?99", n, re.I) and n.lower().endswith((".htm", ".html"))]
        return sorted(names)[:limit]
