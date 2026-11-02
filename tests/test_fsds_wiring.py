"""FSDS wired into the briefing: preferred over R-page scraping, with an automatic fallback."""
import json
import threading

from briefing.graphs.artifact import build_artifact_graph
from briefing.graphs.brief import build_brief_graph
from briefing.models import Artifact, Filing
from briefing.parsing.fsds_rows import column_name, fmt, fsds_records, line_record, month_label, scale_for
from briefing.run import stream_brief
from briefing.sources.fsds import StatementLine

from .fakes import ACC_K, ACC_Q, FakeEdgar, ScriptedSummarizer, doc_claim, make_fsds, make_services, sheet_claim

FILING = Filing(cik="1234567", accession=ACC_K, form="10-K", filed="2026-02-27", report_date="", primary_doc="x.htm")


def brief(svc):
    events = list(stream_brief(svc, build_brief_graph(svc), "ACME"))
    return next(e for e in events if e["type"] == "result")["brief"]


def test_scale_is_exact_never_rounded():
    assert scale_for([109_417_000_000.0, 94_036_000_000.0], "USD") == (1e6, "USD millions")
    assert scale_for([1_234_000.0, 5_000.0], "USD") == (1e3, "USD thousands")
    assert scale_for([1_234_567.0], "USD") == (1.0, "USD")  # not an even thousand: stays in dollars
    assert scale_for([14_776_353_000.0], "shares") == (1e3, "shares in thousands")
    assert scale_for([2.02], "USD/shares") == (1.0, "USD/shares")
    assert (fmt(109417.0), fmt(2.0213), fmt(-1234.5), fmt(0.0)) == ("109,417", "2.0213", "-1,234.5", "0")


def test_column_names_show_the_month_never_a_day():
    """The SEC rounds period ends to month end (Apple's quarter ended Jun 27 shows as Jun 30)."""
    assert column_name("2026-06-30", 1) == "3 months ended Jun 2026"
    assert column_name("2026-06-30", 3) == "9 months ended Jun 2026"
    assert column_name("2025-12-31", 4) == "12 months ended Dec 2025"
    assert column_name("2025-12-31", 0) == "As of Dec 2025"
    assert month_label("not-a-date") == "not-a-date"


def test_line_record_carries_rfile_line_label_tag_units_and_url():
    line = StatementLine(2, 7, "Net sales", "Revenues", False, {("2026-06-30", 1): 109_417e6, ("2025-06-30", 1): 94_036e6}, "USD")
    r = line_record(line, "Income statement", FILING, "ACME")
    assert (r.file, r.row, r.origin, r.source_type) == ("R2.htm", 7, "fsds", "sheet")
    assert json.loads(r.cells) == {"3 months ended Jun 2026": "109,417", "3 months ended Jun 2025": "94,036"}  # newest first
    assert r.text == "Income statement | R2 line 7 | Net sales | 3 months ended Jun 2026: 109,417 | 3 months ended Jun 2025: 94,036 | tag: Revenues | units: USD millions | periods: month level"
    assert r.url == f"{FILING.base_url}/R2.htm" and r.id == f"{FILING.acc_nodash}:sheet:fsds:R2:0007"
    assert r.citation_label() == "10-K · Income statement (R2.htm) · line 7: Net sales · filed 2026-02-27"


def test_negated_label_is_flagged_in_the_text():
    line = StatementLine(7, 12, "Payments for dividends", "PaymentsOfDividends", True, {("2025-12-31", 4): 15e6}, "USD")
    assert "negated label" in line_record(line, "Cash flow statement", FILING, "ACME").text


def test_fsds_records_use_consolidated_figures_only(tmp_path):
    recs = fsds_records(make_fsds(tmp_path), FILING, "ACME")
    by_label = {r.text.split(" | ")[2]: r for r in recs}
    assert set(by_label) == {"Net sales", "Net income", "Diluted (in dollars per share)", "Cash and cash equivalents"}
    assert json.loads(by_label["Net sales"].cells) == {"12 months ended Dec 2025": "1,200", "12 months ended Dec 2024": "1,000"}  # member slice (600) excluded
    assert json.loads(by_label["Net income"].cells) == {"12 months ended Dec 2025": "300,500"}  # 300,500,000 is an even thousand, not million
    assert "units: USD thousands" in by_label["Net income"].text and "negated" in by_label["Net income"].text
    assert json.loads(by_label["Diluted (in dollars per share)"].cells) == {"12 months ended Dec 2025": "2.0213"}
    assert json.loads(by_label["Cash and cash equivalents"].cells) == {"As of Dec 2025": "450"}


def test_fsds_is_preferred_and_r_pages_are_only_fetched_for_filings_it_lacks(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: [doc_claim(r), sheet_claim(r)]), fsds=make_fsds(tmp_path, accessions=(ACC_K,)))
    b = brief(svc)
    k_base, q_base = (f"https://www.sec.gov/Archives/edgar/data/1234567/{a.replace('-', '')}" for a in (ACC_K, ACC_Q))
    assert svc.store.has(ACC_K, "fsds") and not svc.store.has(ACC_K, "sec_statement")
    assert svc.store.has(ACC_Q, "sec_statement") and not svc.store.has(ACC_Q, "fsds")  # 10-Q is not in the data set
    assert not [u for u in svc.edgar.requests if u.startswith(k_base) and u.endswith(("FilingSummary.xml", "R2.htm", "R4.htm"))]
    assert f"{q_base}/FilingSummary.xml" in svc.edgar.requests
    assert set(b["meta"]["figure_sources"]) <= {"fsds", "sec_statement"}


def test_cited_fsds_row_has_line_label_and_link_to_the_r_page(tmp_path):
    only_fsds = ScriptedSummarizer(draft_fn=lambda reg, bf: [_fsds_claim(reg)])
    svc = make_services(tmp_path, only_fsds, fsds=make_fsds(tmp_path, accessions=(ACC_K, ACC_Q)))
    src = brief(svc)["sources"][0]
    assert src["kind"] == "sheet" and src["file"].startswith("R") and src["url"].endswith(".htm")
    assert ": " in src["label"] and "line" in src["label"] and src["row"] == int(src["label"].split("line ")[1].split(":")[0])


def _fsds_claim(registry):
    from briefing.llm import DraftCitation, DraftClaim

    alias, e = next((a, e) for a, e in registry.items() if e["origin"] == "fsds")
    return DraftClaim(facet="financials", text="A figure.", citations=[DraftCitation(source_id=alias, quote=next(iter(json.loads(e["cells"]).values())))])


def test_a_filing_in_the_data_set_without_usable_lines_falls_back_to_r_pages(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: []), fsds=make_fsds(tmp_path, accessions=(), empty=(ACC_Q,)))
    f = next(f for f in svc.edgar.select_filings(svc.edgar.recent_filings("1234567")) if f.accession == ACC_Q)
    out = build_artifact_graph(svc).invoke({"artifact": Artifact("fsds", "ACME", f, url=f.base_url, label="10-Q")})
    assert out["indexed"] > 0 and {r.origin for r in out["records"]} == {"sec_statement"}


def test_without_a_database_everything_still_works_via_r_pages(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: [doc_claim(r), sheet_claim(r)]), fsds=None)
    b = brief(svc)
    assert svc.store.has(ACC_K, "sec_statement") and not svc.store.has(ACC_K, "fsds")
    assert b["meta"]["figure_sources"] == ["sec_statement"]


def test_store_is_usable_from_worker_threads_and_reports_coverage(tmp_path):
    store = make_fsds(tmp_path)
    result: list = []
    t = threading.Thread(target=lambda: result.append((store.has_filing(ACC_K), store.has_filing("nope"), len(store.statement(ACC_K, "IS")))))
    t.start()
    t.join()
    assert result == [(True, False, 3)]
    cov = store.coverage()
    assert cov == {"filings": 1, "filed_from": "2026-02-27", "filed_to": "2026-02-27", "quarters": ["2026q1"]}
    assert isinstance(FakeEdgar, type)  # keep the import honest for linters
