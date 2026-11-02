"""SEC Financial Statement Data Sets (FSDS): bulk XBRL figures loaded into a local SQLite file.

The SEC publishes one zip per calendar quarter (sub/num/pre/tag tab-delimited text) holding the
numbers from the primary financial statements of every XBRL filing, "as filed". This module
loads the 10-K/10-Q family into SQLite and answers the two questions the briefing needs:
which filings are the latest for a company, and what the consolidated statement lines say.

Things worth knowing before trusting a number from here:
  * The SEC file has no ticker column. Ticker -> CIK comes from SEC's company_tickers.json
    (see `load_tickers`), never from guessing at XBRL instance file names.
  * `num` rows with a non-empty `segments` or `coreg` are member/co-registrant slices. They are
    kept, but `statement()` only returns the consolidated figure (both empty).
  * Values are "as filed": an amended filing is a separate row set, flagged by `prevrpt`.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

csv.field_size_limit(10**7)  # tag.txt definitions can exceed the 128 KB default

DEFAULT_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A")

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    adsh TEXT PRIMARY KEY, cik TEXT NOT NULL, name TEXT NOT NULL, sic TEXT, form TEXT NOT NULL,
    period TEXT, fy TEXT, fp TEXT, fye TEXT, filed TEXT NOT NULL, accepted TEXT,
    prevrpt INTEGER NOT NULL, afs TEXT, instance TEXT, quarter TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS filings_cik ON filings (cik, filed);
CREATE TABLE IF NOT EXISTS facts (
    adsh TEXT NOT NULL, tag TEXT NOT NULL, version TEXT NOT NULL, ddate TEXT NOT NULL,
    qtrs INTEGER NOT NULL, uom TEXT NOT NULL, segments TEXT NOT NULL, coreg TEXT NOT NULL,
    value REAL, footnote TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_adsh_tag ON facts (adsh, tag, version);
CREATE TABLE IF NOT EXISTS lines (
    adsh TEXT NOT NULL, report INTEGER NOT NULL, line INTEGER NOT NULL, stmt TEXT NOT NULL,
    inpth INTEGER NOT NULL, rfile TEXT, tag TEXT NOT NULL, version TEXT NOT NULL,
    plabel TEXT NOT NULL, negating INTEGER NOT NULL, PRIMARY KEY (adsh, report, line)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tags (
    tag TEXT NOT NULL, version TEXT NOT NULL, custom INTEGER NOT NULL, abstract INTEGER NOT NULL,
    datatype TEXT, iord TEXT, crdr TEXT, tlabel TEXT, PRIMARY KEY (tag, version)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tickers (ticker TEXT PRIMARY KEY, cik TEXT NOT NULL, title TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tickers_cik ON tickers (cik);
-- Newest 10-K and newest 10-Q per company; an amendment (10-K/A) wins if it is newer.
CREATE VIEW IF NOT EXISTS latest_filings AS
SELECT * FROM (
    SELECT f.*, ROW_NUMBER() OVER (
        PARTITION BY cik, CASE WHEN form LIKE '10-K%' THEN '10-K' ELSE '10-Q' END
        ORDER BY filed DESC, accepted DESC, adsh DESC) AS rn
    FROM filings f
) WHERE rn = 1;
"""


class FsdsError(RuntimeError):
    pass


@dataclass
class QuarterStats:
    quarter: str
    filings: int = 0
    facts: int = 0
    lines: int = 0
    tags: int = 0
    rejected: int = 0  # num rows that could not be parsed (bad field count / non-numeric value)
    repaired: int = 0  # num rows with a stray tab inside `segments`, rebuilt from the tail fields
    warnings: list[str] = field(default_factory=list)


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _iso(d: str) -> str:
    """'20260327' -> '2026-03-27' (the format Filing.filed uses). Anything else passes through."""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def _read(path: Path, required: Iterable[str]) -> tuple[dict[str, int], Iterator[list[str]]]:
    """Tab-delimited, no quoting: the files never quote, and fields contain literal double quotes."""
    fh = open(path, newline="", encoding="utf-8")
    reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if header is None:
        fh.close()
        raise FsdsError(f"{path} is empty")
    idx = {name: i for i, name in enumerate(header)}
    missing = [c for c in required if c not in idx]
    if missing:
        fh.close()
        raise FsdsError(f"{path.name} is missing expected columns {missing}; is this an FSDS file?")

    def rows() -> Iterator[list[str]]:
        with fh:
            yield from reader

    return idx, rows()


def _bool(s: str) -> int:
    return 1 if s.strip() == "1" else 0


def ingest_quarter(conn: sqlite3.Connection, raw_dir: str | Path, quarter: str, forms: Iterable[str] = DEFAULT_FORMS) -> QuarterStats:
    """Load one extracted FSDS quarter. Re-running a quarter replaces its rows (idempotent)."""
    raw = Path(raw_dir)
    wanted = set(forms)
    stats = QuarterStats(quarter)

    sub_idx, sub_rows = _read(raw / "sub.txt", ["adsh", "cik", "name", "form", "filed", "prevrpt"])
    filings = []
    for r in sub_rows:
        if len(r) != len(sub_idx) or r[sub_idx["form"]] not in wanted:
            continue
        g = lambda c: r[sub_idx[c]]  # noqa: E731 - tiny local accessor
        filings.append((g("adsh"), g("cik"), g("name"), g("sic"), g("form"), _iso(g("period")), g("fy"), g("fp"), g("fye"),
                        _iso(g("filed")), g("accepted").removesuffix(".0"), _bool(g("prevrpt")), g("afs"), g("instance"), quarter))
    adsh_set = {f[0] for f in filings}
    stats.filings = len(filings)

    with conn:  # one transaction per quarter: a failure leaves the previous state intact
        _purge_quarter(conn, quarter)
        conn.executemany("INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", filings)
        stats.facts, stats.rejected, stats.repaired = _load_num(conn, raw / "num.txt", adsh_set)
        stats.lines = _load_pre(conn, raw / "pre.txt", adsh_set)
        stats.tags = _load_tags(conn, raw / "tag.txt", adsh_set)
    if stats.rejected:
        stats.warnings.append(f"{stats.rejected} num rows could not be parsed and were skipped")
    return stats


def _purge_quarter(conn: sqlite3.Connection, quarter: str) -> None:
    adsh = "SELECT adsh FROM filings WHERE quarter = ?"
    conn.execute(f"DELETE FROM facts WHERE adsh IN ({adsh})", (quarter,))
    conn.execute(f"DELETE FROM lines WHERE adsh IN ({adsh})", (quarter,))
    conn.execute("DELETE FROM filings WHERE quarter = ?", (quarter,))


def _batched(rows: Iterable[tuple], size: int = 50_000) -> Iterator[list[tuple]]:
    batch: list[tuple] = []
    for r in rows:
        batch.append(r)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _load_num(conn: sqlite3.Connection, path: Path, adsh_set: set[str]) -> tuple[int, int, int]:
    idx, rows = _read(path, ["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value", "footnote"])
    n = len(idx)
    counts = {"loaded": 0, "rejected": 0, "repaired": 0}

    def gen() -> Iterator[tuple]:
        for r in rows:
            if not r or r[idx["adsh"]] not in adsh_set:
                continue
            if len(r) != n:
                # A custom member name can contain a literal tab, which splits `segments`. The last
                # three columns are always coreg, value, footnote, so the middle is rejoinable.
                if len(r) < n:
                    counts["rejected"] += 1
                    continue
                r = r[: idx["segments"]] + ["\t".join(r[idx["segments"] : len(r) - 3])] + r[-3:]
                counts["repaired"] += 1
            try:
                value = float(r[idx["value"]]) if r[idx["value"]] else None
                qtrs = int(r[idx["qtrs"]])
            except ValueError:
                counts["rejected"] += 1
                continue
            counts["loaded"] += 1
            yield (r[idx["adsh"]], r[idx["tag"]], r[idx["version"]], _iso(r[idx["ddate"]]), qtrs, r[idx["uom"]],
                   r[idx["segments"]], r[idx["coreg"]], value, r[idx["footnote"]])

    for batch in _batched(gen()):
        conn.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    return counts["loaded"], counts["rejected"], counts["repaired"]


def _load_pre(conn: sqlite3.Connection, path: Path, adsh_set: set[str]) -> int:
    idx, rows = _read(path, ["adsh", "report", "line", "stmt", "inpth", "rfile", "tag", "version", "plabel", "negating"])
    n = len(idx)
    total = 0

    def gen() -> Iterator[tuple]:
        nonlocal total
        for r in rows:
            if len(r) != n or r[idx["adsh"]] not in adsh_set:
                continue
            total += 1
            yield (r[idx["adsh"]], int(r[idx["report"]]), int(r[idx["line"]]), r[idx["stmt"]], _bool(r[idx["inpth"]]),
                   r[idx["rfile"]], r[idx["tag"]], r[idx["version"]], r[idx["plabel"]], _bool(r[idx["negating"]]))

    for batch in _batched(gen()):
        conn.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    return total


def _load_tags(conn: sqlite3.Connection, path: Path, adsh_set: set[str]) -> int:
    """Standard tags (shared across quarters, so deduped) plus custom tags defined by loaded filings."""
    idx, rows = _read(path, ["tag", "version", "custom", "abstract", "datatype", "iord", "crdr", "tlabel"])
    n = len(idx)
    before = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]

    def gen() -> Iterator[tuple]:
        for r in rows:
            if len(r) != n:
                continue
            custom = _bool(r[idx["custom"]])
            if custom and r[idx["version"]] not in adsh_set:
                continue
            yield (r[idx["tag"]], r[idx["version"]], custom, _bool(r[idx["abstract"]]), r[idx["datatype"]] or None,
                   r[idx["iord"]] or None, r[idx["crdr"]] or None, r[idx["tlabel"]] or None)

    for batch in _batched(gen()):
        conn.executemany("INSERT OR IGNORE INTO tags VALUES (?,?,?,?,?,?,?,?)", batch)
    return conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] - before


def load_tickers(conn: sqlite3.Connection, path: str | Path) -> int:
    """Load SEC's company_tickers.json ({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...})."""
    data = json.loads(Path(path).read_text())
    rows = [(v["ticker"].upper(), str(int(v["cik_str"])), v["title"]) for v in data.values()]
    with conn:
        conn.execute("DELETE FROM tickers")
        conn.executemany("INSERT OR REPLACE INTO tickers VALUES (?,?,?)", rows)
    return len(rows)


@dataclass(frozen=True)
class StatementLine:
    report: int  # the EDGAR "R file" number: report=4 is R4.htm
    line: int  # presentation order within that report
    label: str  # the filer's own line label
    tag: str
    negating: bool
    values: dict[tuple[str, int], float]  # (period end date, quarters covered) -> value; qtrs=0 is a point in time
    uom: str


class FsdsStore:
    """Read side. Open the SQLite file the ingest script produced.

    The graph runs nodes on worker threads and sqlite3 connections are thread-bound, so each
    thread gets its own read-only connection (cheap: the file is only opened, never copied).
    """

    def __init__(self, path: str | Path):
        path = Path(path)
        if not path.exists():
            raise FsdsError(f"{path} does not exist. Run scripts/ingest_fsds.py first.")
        self._path = path
        self._local = threading.local()
        self.conn  # noqa: B018 - open now so a corrupt file fails at startup, not mid-request

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def has_filing(self, adsh: str) -> bool:
        return self.conn.execute("SELECT 1 FROM filings WHERE adsh = ?", (adsh,)).fetchone() is not None

    def coverage(self) -> dict:
        """What the loaded data covers, for the health endpoint and for honest UI messaging."""
        row = self.conn.execute("SELECT COUNT(*) AS n, MIN(filed) AS lo, MAX(filed) AS hi FROM filings").fetchone()
        quarters = [r["quarter"] for r in self.conn.execute("SELECT DISTINCT quarter FROM filings ORDER BY quarter")]
        return {"filings": row["n"], "filed_from": row["lo"], "filed_to": row["hi"], "quarters": quarters}

    def cik_for_ticker(self, ticker: str) -> str | None:
        row = self.conn.execute("SELECT cik FROM tickers WHERE ticker = ?", (ticker.strip().upper().replace(".", "-"),)).fetchone()
        return row["cik"] if row else None

    def latest_filings(self, cik: str) -> list[dict]:
        """Newest 10-K and newest 10-Q for the company, newest first."""
        rows = self.conn.execute("SELECT * FROM latest_filings WHERE cik = ? ORDER BY filed DESC", (str(int(cik)),)).fetchall()
        return [dict(r) for r in rows]

    def filing_url(self, adsh: str) -> str:
        row = self.conn.execute("SELECT cik FROM filings WHERE adsh = ?", (adsh,)).fetchone()
        if row is None:
            raise FsdsError(f"unknown accession {adsh}")
        return f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{adsh.replace('-', '')}/"

    def statement(self, adsh: str, stmt: str) -> list[StatementLine]:
        """Consolidated lines of one statement (stmt: BS, IS, CF, EQ, CI), in presentation order.

        Only lines that carry at least one consolidated figure are returned, and parenthetical
        lines are excluded. Member slices (segments) are not attached to a line here.
        """
        cur = self.conn.execute(
            """SELECT p.report, p.line, p.plabel, p.tag, p.negating, n.ddate, n.qtrs, n.uom, n.value
               FROM lines p JOIN facts n
                 ON n.adsh = p.adsh AND n.tag = p.tag AND n.version = p.version AND n.segments = '' AND n.coreg = ''
               WHERE p.adsh = ? AND p.stmt = ? AND p.inpth = 0 AND n.value IS NOT NULL
               ORDER BY p.report, p.line, n.ddate DESC, n.qtrs""",
            (adsh, stmt),
        )
        out: dict[tuple[int, int], StatementLine] = {}
        for r in cur:
            key = (r["report"], r["line"])
            if key not in out:
                out[key] = StatementLine(r["report"], r["line"], r["plabel"], r["tag"], bool(r["negating"]), {}, r["uom"])
            out[key].values[(r["ddate"], r["qtrs"])] = r["value"]
        return list(out.values())
