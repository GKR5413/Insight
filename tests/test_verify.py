import json

from briefing.models import Record
from briefing.verify import verify_citation, verify_claim, verify_doc_quote

PASSAGE = "The Company’s revenue increased 16 percent year over year,   driven by higher net sales of iPhone and Services."


def doc(text=PASSAGE):
    return Record(id="d", ticker="T", accession="a", form="10-K", filed="2025-01-01", source_type="doc", section_key="10-K:7", item="Item 7", text=text)


def sheet(cells=None):
    cells = cells or {"3 Months Ended Jun. 27, 2026": "$ 109,417", "3 Months Ended Jun. 28, 2025": "$ 94,036"}
    return Record(
        id="s", ticker="T", accession="a", form="10-Q", filed="2026-07-31", source_type="sheet", section_key="10-Q:SHEET", item="OPERATIONS",
        text="OPERATIONS | row 3 | Net sales | " + " | ".join(f"{k}: {v}" for k, v in cells.items()), file="R2.htm", sheet="OPERATIONS", row=3,
        cells=json.dumps(cells), origin="sec_statement",
    )


def test_doc_quote_survives_curly_quotes_spacing_and_case():
    assert verify_doc_quote(PASSAGE, "the company's REVENUE increased 16 percent year over year, driven by higher net sales").ok


def test_doc_quote_not_in_source_fails_with_reason():
    r = verify_doc_quote(PASSAGE, "revenue decreased sharply because of tariffs on imported components")
    assert not r.ok and "not found" in r.reason


def test_doc_quote_too_short_cannot_verify_anything():
    assert not verify_doc_quote(PASSAGE, "16 percent").ok


def test_doc_quote_with_ellipsis_checks_each_fragment():
    assert verify_doc_quote(PASSAGE, "revenue increased 16 percent … higher net sales of iPhone and Services").ok
    assert not verify_doc_quote(PASSAGE, "revenue increased 16 percent … and also launched a new product line").ok


def test_sheet_quote_checks_figures_against_the_cited_row():
    r = sheet()
    assert verify_citation(r, "$ 109,417").ok
    assert verify_citation(r, "109,417 and 94,036").ok
    bad = verify_citation(r, "$ 109,418")
    assert not bad.ok and "row 3" in bad.reason


def test_sheet_quote_may_restate_period_headings_but_needs_a_figure():
    r = sheet()
    assert verify_citation(r, "Jun. 27, 2026: $ 109,417").ok  # 27 and 2026 are heading text, not claims
    assert not verify_citation(r, "Net sales, Jun. 27, 2026").ok  # no figure at all


def test_sheet_quote_does_not_accept_unit_converted_figures():
    assert not verify_citation(sheet(), "$109.4 billion").ok


def test_claim_level_rules():
    reg = {"S1": doc(), "S2": sheet()}
    good = {"text": "Revenue grew.", "citations": [{"source_id": "S1", "quote": "revenue increased 16 percent year over year"}]}
    assert verify_claim(good, reg).ok
    assert not verify_claim({"text": "x", "citations": []}, reg).ok
    unknown = verify_claim({"text": "x", "citations": [{"source_id": "S9", "quote": "whatever whatever whatever"}]}, reg)
    assert not unknown.ok and "S9" in unknown.reasons[0]
    mixed = {"text": "x", "citations": [good["citations"][0], {"source_id": "S2", "quote": "$ 1"}]}
    assert not verify_claim(mixed, reg).ok  # one bad citation fails the whole claim
