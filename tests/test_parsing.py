import json

import openpyxl

from briefing.models import Record
from briefing.parsing.html_filing import parse_exhibit_html, parse_filing_html, section_passages, sections_to_records
from briefing.parsing.statements import parse_number, parse_statement_html
from briefing.parsing.xlsx_upload import workbook_records
from briefing.models import Filing

from .fakes import OPERATIONS, eightk_html, exhibit_html, tenk_html, tenq_html

FILING = Filing(cik="1", accession="0000000001-25-000001", form="10-K", filed="2025-01-01", report_date="", primary_doc="x.htm")


def test_10k_segments_items_and_toc_loses_to_real_section():
    secs = parse_filing_html(tenk_html(), "10-K")
    assert [s.key for s in secs] == ["1", "1A", "7"]
    assert secs[0].label == "Item 1 — Business"
    assert secs[2].label.startswith("Item 7 — Management’s Discussion")
    text = " ".join(b.text for s in secs for b in s.blocks)
    assert "HIDDEN XBRL" not in text  # ix:header is stripped
    assert "Pursuant to the requirements" not in text  # signature block is cut


def test_cross_reference_sentence_is_not_a_heading():
    secs = {s.key: s for s in parse_filing_html(tenk_html(), "10-K")}
    assert any("See Item 1A of this Form 10-K" in b.text for b in secs["7"].blocks)


def test_data_tables_stay_atomic_with_merged_currency_cells():
    s7 = next(s for s in parse_filing_html(tenk_html(), "10-K") if s.key == "7")
    tables = [b.text for b in s7.blocks if b.is_table]
    assert len(tables) == 1
    assert "Total net sales | $1,200 | $1,000" in tables[0]
    passages = section_passages(s7)
    assert sum(p.startswith("[Table]") for p in passages) == 1


def test_10q_keys_include_the_part():
    secs = parse_filing_html(tenq_html(), "10-Q")
    assert [s.key for s in secs] == ["I.2", "II.1A"]
    assert secs[0].label.startswith("Part I, Item 2 — Management’s Discussion")


def test_8k_skips_exhibit_list_item_and_keeps_results_item():
    secs = parse_filing_html(eightk_html(), "8-K")
    assert [s.key for s in secs] == ["2.02"]
    assert secs[0].label == "Item 2.02 — Results of Operations and Financial Condition"


def test_records_have_stable_ids_filters_and_context_header():
    secs = parse_filing_html(tenk_html(), "10-K")
    a = sections_to_records(secs, FILING, "ACME", "http://u")
    b = sections_to_records(parse_filing_html(tenk_html(), "10-K"), FILING, "ACME", "http://u")
    assert [r.id for r in a] == [r.id for r in b]
    assert len({r.id for r in a}) == len(a)
    r = a[0]
    assert r.section_key == "10-K:1" and r.source_type == "doc"
    assert r.embed_text().startswith("ACME | 10-K | filed 2025-01-01 | Item 1 — Business\n")
    assert r.citation_label() == "10-K · Item 1 — Business · filed 2025-01-01"


def test_exhibit_parsing_labels_99_1():
    recs = parse_exhibit_html(exhibit_html(), FILING, "ACME", "http://u", "acme-ex991.htm")
    assert recs and recs[0].section_key == "10-K:EX99.1"
    assert recs[0].item.startswith("Exhibit 99.1")


def test_statement_headers_align_despite_rowspan():
    """Regression: the title cell has rowspan=2, so a naive parser shifts every date one column."""
    units, rows = parse_statement_html(OPERATIONS, "CONSOLIDATED STATEMENTS OF OPERATIONS")
    assert units == "USD ($) $ in Millions"
    net_sales = next(r for r in rows if r.label == "Net sales")
    assert net_sales.row == 3  # 1-based position in the rendered table
    assert net_sales.cells == {"12 Months Ended Sep. 27, 2025": "$ 1,200", "12 Months Ended Sep. 28, 2024": "$ 1,000"}
    assert "units: USD ($) $ in Millions" in net_sales.text


def test_parse_number_handles_parentheses_currency_and_percent():
    assert parse_number("$ (1,234.5)") == -1234.5
    assert parse_number("12%") == 12.0
    assert parse_number("Jun. 27, 2026") is None


def test_xlsx_rows_use_excel_visible_row_numbers(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Acme model"  # title row
    ws.append([])  # row 2 blank
    ws.append(["Metric", "FY2024", "FY2025"])  # row 3 = header
    ws.append(["Revenue", 1000, 1200.5])  # row 4
    ws.append(["EBITDA", None, 300])  # row 5
    ws.append([])  # row 6 blank
    ws.append(["Net margin", 0.25, 0.3])  # row 7
    path = tmp_path / "model.xlsx"
    wb.save(path)
    recs = workbook_records(path, "ACME")
    assert [(r.label if hasattr(r, "label") else r.text.split(" | ")[2], r.row) for r in recs] == [("Revenue", 4), ("EBITDA", 5), ("Net margin", 7)]
    rev = recs[0]
    assert json.loads(rev.cells) == {"FY2024": "1,000", "FY2025": "1,200.5"}
    assert json.loads(recs[1].cells) == {"FY2025": "300"}
    assert rev.citation_label() == "model.xlsx · Model · row 4"
    assert rev.accession.startswith("upload-") and rev.origin == "xlsx_upload"
    assert isinstance(rev, Record)
