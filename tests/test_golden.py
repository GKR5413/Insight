from decimal import Decimal
from pathlib import Path

import pytest

from briefing.golden import (
    Golden, build_pack, evidence_in_pack, find_companies, format_pack, load_doc_chunks, load_golden,
    parse_amounts, parse_dates, pick_doc_chunks, score_answer,
)
from briefing.sources.fsds import FsdsStore, connect, ingest_quarter

from .test_fsds import K1, sub_row, write_quarter

GOLDEN = Path(__file__).parent / "golden" / "sec-golden.jsonl"


def test_the_shipped_golden_file_loads():
    golden = load_golden(GOLDEN)
    assert len(golden) == 15 and golden[0].id == "SEC-01"
    assert golden[-1].expected.startswith("Insufficient evidence") and golden[-1].evidence == ()


def test_bad_golden_lines_are_reported_with_their_line_number(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text('{"id": "A", "question": "q", "expected_answer": "x"}\n{"id": "B"}\n')
    with pytest.raises(ValueError, match=r"g\.jsonl:2"):
        load_golden(p)
    p.write_text('{"id": "A", "question": "q", "expected_answer": "x"}\n' * 2)
    with pytest.raises(ValueError, match="duplicate ids"):
        load_golden(p)


@pytest.mark.parametrize("answer", ["2026-01-30", "20260130", "January 30, 2026", "Jan 30th, 2026", "30 January 2026", "1/30/2026", "Filed on 2026-01-30."])
def test_dates_are_accepted_in_any_common_format(answer):
    assert score_answer("20260130", answer, False).correct


def test_wrong_or_missing_dates_fail():
    assert not score_answer("20260130", "2026-01-31", False).correct
    assert not score_answer("20260130", "late January", False).correct
    assert parse_dates("2026-02-30") == set()  # impossible date is not a date


@pytest.mark.parametrize("answer", ["143,756,000,000 USD", "$143,756,000,000", "143756000000", "$143.756 billion", "143,756 million", "About 143.756 billion dollars"])
def test_amounts_accept_scale_words_and_formats(answer):
    assert score_answer("143,756,000,000 USD", answer, False).correct


def test_amounts_are_strict_about_the_figure_and_the_sign():
    assert not score_answer("143,756,000,000 USD", "143,757,000,000 USD", False).correct
    assert not score_answer("143,756,000,000 USD", "-143,756,000,000 USD", False).correct
    for answer in ("-1,767,000,000 USD", "−$1,767,000,000", "$-1.767 billion", "(1,767,000,000)", "Change: -1,767,000,000"):
        assert score_answer("-1,767,000,000 USD", answer, False).correct, answer
    assert not score_answer("-1,767,000,000 USD", "1,767,000,000 USD", False).correct  # lost the sign


def test_a_spaced_dash_or_quarter_name_is_not_a_minus_sign():
    assert Decimal(111_184_000_000) in parse_amounts("Q2 - 111,184,000,000 USD")
    assert Decimal(-111_184_000_000) not in parse_amounts("Q2 - 111,184,000,000 USD")
    assert Decimal(-3) not in parse_amounts("Q2-Q3 and 2026-01-30")


def test_labels_must_match_not_merely_contain():
    assert score_answer("Net sales", "Net sales", False).correct
    assert score_answer("Net sales", '"Net sales".', False).correct
    assert not score_answer("Net sales", "Total net sales, including services and products revenue recognised in the period", False).correct
    assert score_answer("In the next quarterly posting.", "They are included in the next quarterly posting.", False).correct


def test_abstention_cases():
    expected = "Insufficient evidence. The supplied files do not report that future quarter."
    assert score_answer(expected, "Insufficient evidence.", True).correct
    assert score_answer(expected, "Insufficient evidence", False).correct  # said so in text without the flag
    assert not score_answer(expected, "Apple will report about 120 billion USD.", False).correct
    assert not score_answer("20260130", "Insufficient evidence.", True).correct  # abstained when it should have answered


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "raw"
    other = "0009999999-26-000002"
    write_quarter(
        root, "2026q1",
        sub=[sub_row(K1, "1234567", "ACME CORP", "10-K", "20260227"), sub_row(other, "9999999", "ACME HOLDINGS CORP", "10-K", "20260301")],
        num=[[K1, "Revenues", "us-gaap/2025", "20251231", "4", "USD", "", "", "1000.0000", ""],
             [K1, "Revenues", "us-gaap/2025", "20241231", "4", "USD", "", "", "900.5000", ""]],
        pre=[[K1, "4", "1", "IS", "0", "H", "Revenues", "us-gaap/2025", "Net revenue", "0"]],
        tag=[["Revenues", "us-gaap/2025", "0", "0", "monetary", "D", "C", "Revenues, total", "doc"]],
    )
    db = tmp_path / "f.sqlite"
    conn = connect(db)
    ingest_quarter(conn, root / "2026q1", "2026q1")
    conn.close()
    return FsdsStore(db)


def test_company_is_matched_by_whole_name_without_suffix_and_not_by_a_longer_name(store):
    assert find_companies(store, "What was Acme’s net revenue for 2025?") == [("1234567", "ACME CORP")]
    assert find_companies(store, "What did Acme Holdings report?") == [("9999999", "ACME HOLDINGS CORP")]
    assert find_companies(store, "How did Pineapple do?") == []  # whole words only


def test_pack_contains_figures_labels_and_period_semantics(store):
    pack = build_pack(store, "What was Acme's net revenue?")
    text = format_pack(pack)
    assert [s.id for s in pack] == ["S1"]
    assert "ACME CORP (CIK 1234567) | Form 10-K | fiscal year 2025, fiscal period FY" in text
    assert 'line 1 "Net revenue" | tag Revenues ("Revenues, total")' in text
    assert "period ending 2025-12-31, qtrs=4: 1,000 USD; period ending 2024-12-31, qtrs=4: 900.5 USD" in text
    assert format_pack(build_pack(store, "How did Nobody Inc do?")) == "(no sources were found for this question)"


def test_evidence_check_flags_what_the_pack_lacks(store):
    text = format_pack(build_pack(store, "What was Acme's net revenue?"))
    good = Golden("G", "q", "1,000 USD", ({"source": "2026q1.zip/num.txt", "match": {"ddate": "20251231", "qtrs": "4", "value": "1000.0000", "uom": "USD", "tag": "Revenues"}},))
    bad = Golden("B", "q", "x", ({"source": "2026q1.zip/num.txt", "match": {"ddate": "20251231", "qtrs": "1", "value": "1000.0000", "uom": "USD", "tag": "Revenues"}},))
    assert evidence_in_pack(good, text) == []
    assert evidence_in_pack(bad, text) == ["2026q1.zip/num.txt Revenues 20251231 qtrs=1"]


def test_doc_chunks_are_picked_by_overlap_and_ignored_when_unrelated(tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text(
        "Overview\n\nData contained in documents filed after the last business day of the quarter will be included in the next quarterly posting. "
        "The information will be updated quarterly.\n\nSIC is a four digit code indicating the registrant's type of business.\n"
    )
    chunks = load_doc_chunks(doc)
    q = "When are filings submitted after the last business day of a quarter included in the data sets?"
    picked = pick_doc_chunks(chunks, q)
    assert picked and "next quarterly posting" in picked[0]
    assert pick_doc_chunks(chunks, "What were total net sales for Apple?") == []
