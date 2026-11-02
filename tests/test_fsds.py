import json

import pytest

from briefing.sources.fsds import FsdsError, FsdsStore, connect, ingest_quarter, load_tickers

SUB_HEAD = "adsh cik name sic countryba stprba cityba zipba bas1 bas2 baph countryma stprma cityma zipma mas1 mas2 countryinc stprinc ein former changed afs wksi fye form period fy fp filed accepted prevrpt detail instance nciks aciks".split()
NUM_HEAD = "adsh tag version ddate qtrs uom segments coreg value footnote".split()
PRE_HEAD = "adsh report line stmt inpth rfile tag version plabel negating".split()
TAG_HEAD = "tag version custom abstract datatype iord crdr tlabel doc".split()


def sub_row(adsh, cik, name, form, filed, period="20251231", fp="FY", prevrpt="0"):
    d = dict.fromkeys(SUB_HEAD, "")
    d.update(adsh=adsh, cik=cik, name=name, sic="3571", form=form, period=period, fy="2025", fp=fp, fye="1231", filed=filed,
             accepted=f"{filed[:4]}-{filed[4:6]}-{filed[6:]} 16:30:00.0", prevrpt=prevrpt, detail="1", instance="acme-20251231_htm.xml", nciks="1", wksi="0")
    return [d[c] for c in SUB_HEAD]


def write_quarter(root, quarter, sub, num, pre, tag):
    q = root / quarter
    q.mkdir(parents=True)
    for name, head, rows in (("sub", SUB_HEAD, sub), ("num", NUM_HEAD, num), ("pre", PRE_HEAD, pre), ("tag", TAG_HEAD, tag)):
        (q / f"{name}.txt").write_text("\n".join("\t".join(r) for r in [head, *rows]) + "\n", encoding="utf-8")
    return q


K1, Q1, K2 = "0001234567-26-000010", "0001234567-26-000040", "0001234567-26-000099"
F20, OTHER = "0009999999-26-000001", "0007654321-26-000005"


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / "raw"
    write_quarter(
        root, "2026q1",
        sub=[
            sub_row(K1, "1234567", "ACME CORP", "10-K", "20260227"),
            sub_row(F20, "9999999", "FOREIGN PLC", "20-F", "20260301"),
            sub_row(OTHER, "7654321", "OTHER INC", "10-Q", "20260310", period="20260131", fp="Q3"),
        ],
        num=[
            [K1, "Revenues", "us-gaap/2025", "20251231", "4", "USD", "", "", "1000.0000", ""],
            [K1, "Revenues", "us-gaap/2025", "20241231", "4", "USD", "", "", "900.0000", ""],
            [K1, "Revenues", "us-gaap/2025", "20251231", "4", "USD", "ProductOrServiceAxis=ProductMember;", "", "600.0000", ""],
            [K1, "Assets", "us-gaap/2025", "20251231", "0", "USD", "", "", "5000.5000", ""],
            [K1, "Assets", "us-gaap/2025", "20251231", "0", "USD", "", "ParentCo", "123.0000", ""],
            [K1, "NetIncomeLoss", "us-gaap/2025", "20251231", "4", "USD", "", "", "", ""],  # NULL value
            [K1, "Weird", "us-gaap/2025", "20251231", "0", "USD", 'Id=Unfunded equity\tsecurities;"', "", "7.0000", ""],  # stray tab + quote
            [K1, "TooShort", "us-gaap/2025", "20251231", "0"],  # short row
            [K1, "BadValue", "us-gaap/2025", "20251231", "0", "USD", "", "", "n/a", ""],
            [F20, "Revenues", "us-gaap/2025", "20251231", "4", "USD", "", "", "1.0000", ""],  # filtered-out form
            [OTHER, "Revenues", "us-gaap/2025", "20260131", "1", "USD", "", "", "50.0000", ""],
        ],
        pre=[
            [K1, "2", "1", "BS", "0", "H", "Assets", "us-gaap/2025", 'Total "assets"', "0"],
            [K1, "4", "1", "IS", "0", "H", "Revenues", "us-gaap/2025", "Net revenue", "0"],
            [K1, "4", "2", "IS", "0", "H", "NetIncomeLoss", "us-gaap/2025", "Net income", "0"],
            [K1, "4", "3", "IS", "1", "H", "Revenues", "us-gaap/2025", "Revenue (parenthetical)", "0"],
            [F20, "2", "1", "BS", "0", "H", "Assets", "us-gaap/2025", "Total assets", "0"],
        ],
        tag=[
            ["Revenues", "us-gaap/2025", "0", "0", "monetary", "D", "C", "Revenues", "doc"],
            ["Custom", K1, "1", "0", "monetary", "D", "", "Acme custom", "doc"],
            ["Custom", F20, "1", "0", "monetary", "D", "", "Not loaded", "doc"],
        ],
    )
    # a later quarter: 10-Q for Acme, then a 10-K/A that is newer than the original 10-K
    write_quarter(
        root, "2026q2",
        sub=[
            sub_row(Q1, "1234567", "ACME CORP", "10-Q", "20260505", period="20260331", fp="Q1"),
            sub_row(K2, "1234567", "ACME CORP", "10-K/A", "20260615", prevrpt="0"),
        ],
        num=[[Q1, "Revenues", "us-gaap/2025", "20260331", "1", "USD", "", "", "260.0000", ""],
             [K2, "Revenues", "us-gaap/2025", "20251231", "4", "USD", "", "", "1010.0000", ""]],
        pre=[[Q1, "4", "1", "IS", "0", "H", "Revenues", "us-gaap/2025", "Net revenue", "0"],
             [K2, "4", "1", "IS", "0", "H", "Revenues", "us-gaap/2025", "Net revenue", "0"]],
        tag=[["Revenues", "us-gaap/2025", "0", "0", "monetary", "D", "C", "Revenues", "doc"]],
    )
    return root


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_only_requested_forms_are_loaded_and_bad_rows_are_counted(raw, tmp_path):
    conn = connect(tmp_path / "f.sqlite")
    s = ingest_quarter(conn, raw / "2026q1", "2026q1")
    assert s.filings == 2  # the 20-F is skipped
    assert not conn.execute("SELECT 1 FROM facts WHERE adsh = ?", (F20,)).fetchone()
    assert not conn.execute("SELECT 1 FROM lines WHERE adsh = ?", (F20,)).fetchone()
    assert (s.rejected, s.repaired) == (2, 1)  # short row + non-numeric value; one stray-tab row rebuilt
    assert s.warnings and "2 num rows" in s.warnings[0]


def test_stray_tab_in_segments_is_repaired_not_shifted(raw, tmp_path):
    conn = connect(tmp_path / "f.sqlite")
    ingest_quarter(conn, raw / "2026q1", "2026q1")
    seg, coreg, value = conn.execute("SELECT segments, coreg, value FROM facts WHERE tag = 'Weird'").fetchone()
    assert seg == 'Id=Unfunded equity\tsecurities;"' and coreg == "" and value == 7.0


def test_ingest_is_idempotent_and_dates_are_iso(raw, tmp_path):
    conn = connect(tmp_path / "f.sqlite")
    ingest_quarter(conn, raw / "2026q1", "2026q1")
    before = {t: count(conn, t) for t in ("filings", "facts", "lines", "tags")}
    ingest_quarter(conn, raw / "2026q1", "2026q1")
    assert {t: count(conn, t) for t in before} == before
    assert conn.execute("SELECT filed, period, accepted FROM filings WHERE adsh = ?", (K1,)).fetchone() == ("2026-02-27", "2025-12-31", "2026-02-27 16:30:00")
    assert conn.execute("SELECT MIN(ddate) FROM facts WHERE adsh = ?", (K1,)).fetchone()[0] == "2024-12-31"
    # standard tags are shared; a custom tag is kept only when a loaded filing defined it
    assert {r[0] for r in conn.execute("SELECT version FROM tags WHERE tag = 'Custom'")} == {K1}


def test_latest_filings_picks_newest_10k_and_10q_and_amendment_wins(raw, tmp_path):
    db = tmp_path / "f.sqlite"
    conn = connect(db)
    for q in ("2026q1", "2026q2"):
        ingest_quarter(conn, raw / q, q)
    conn.close()
    store = FsdsStore(db)
    latest = store.latest_filings("1234567")
    assert [(f["form"], f["adsh"]) for f in latest] == [("10-K/A", K2), ("10-Q", Q1)]
    assert store.latest_filings("0001234567") == latest  # leading zeros tolerated
    assert store.latest_filings("42") == []
    assert store.filing_url(K2) == "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000099/"
    with pytest.raises(FsdsError, match="unknown accession"):
        store.filing_url("nope")


def test_statement_returns_consolidated_lines_in_presentation_order(raw, tmp_path):
    db = tmp_path / "f.sqlite"
    conn = connect(db)
    ingest_quarter(conn, raw / "2026q1", "2026q1")
    conn.close()
    store = FsdsStore(db)
    inc = store.statement(K1, "IS")
    # NetIncomeLoss has only a NULL value and the parenthetical line is excluded; member slice is not mixed in
    assert [(l.report, l.line, l.label, l.tag) for l in inc] == [(4, 1, "Net revenue", "Revenues")]
    assert inc[0].values == {("2025-12-31", 4): 1000.0, ("2024-12-31", 4): 900.0}
    bs = store.statement(K1, "BS")
    assert [(l.label, l.values) for l in bs] == [('Total "assets"', {("2025-12-31", 0): 5000.5})]  # co-registrant row ignored


def test_ticker_lookup_uses_the_sec_file(raw, tmp_path):
    db = tmp_path / "f.sqlite"
    conn = connect(db)
    tickers = tmp_path / "company_tickers.json"
    tickers.write_text(json.dumps({"0": {"cik_str": 1234567, "ticker": "ACME", "title": "Acme Corp"},
                                   "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE"}}))
    assert load_tickers(conn, tickers) == 2
    conn.close()
    store = FsdsStore(db)
    assert store.cik_for_ticker("acme") == "1234567"
    assert store.cik_for_ticker("BRK.B") == "1067983"  # same normalisation as EdgarClient.resolve
    assert store.cik_for_ticker("NOPE") is None


def test_wrong_files_fail_loudly(tmp_path):
    with pytest.raises(FsdsError, match="Run scripts/ingest_fsds.py"):
        FsdsStore(tmp_path / "missing.sqlite")
    bad = tmp_path / "2026q1"
    bad.mkdir()
    (bad / "sub.txt").write_text("a\tb\n1\t2\n")
    with pytest.raises(FsdsError, match="missing expected columns"):
        ingest_quarter(connect(tmp_path / "f.sqlite"), bad, "2026q1")
