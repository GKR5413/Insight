"""SEC Financial Statement Data Sets lines -> sheet-style evidence records.

One record per statement line, carrying the EDGAR R-file number and the data set's presentation
line so an advisor can open the SEC page and find it. Numbers are exact: a line is shown in
millions (or thousands) only when every value divides evenly, never rounded, so a quote such as
"109,417" can be checked cell-for-cell. The SEC rounds period-end dates to month ends in this
data set, and the column headings say so rather than pretending otherwise.
"""
from __future__ import annotations

import json

from ..models import Filing, Record
from ..sources.fsds import FsdsStore, StatementLine

STATEMENTS: tuple[tuple[str, str], ...] = (
    ("IS", "Income statement"),
    ("BS", "Balance sheet"),
    ("CF", "Cash flow statement"),
)
MAX_COLUMNS = 6


def scale_for(values: list[float], uom: str) -> tuple[float, str]:
    """Largest exact scale for a line: (divisor, units label)."""
    if uom == "USD":
        for div, name in ((1e6, "USD millions"), (1e3, "USD thousands")):
            if all(v % div == 0 for v in values):
                return div, name
        return 1.0, "USD"
    if uom == "shares" and all(v % 1e3 == 0 for v in values):
        return 1e3, "shares in thousands"
    return 1.0, uom or "units"


def fmt(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return f"{round(x):,}"
    return f"{x:,.4f}".rstrip("0").rstrip(".")


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_label(ddate: str) -> str:
    """'2026-06-30' -> 'Jun 2026'. The SEC rounds period ends to the nearest month end, so showing a
    day would claim precision the data set does not have (Apple's quarter ended Jun 27, not Jun 30)."""
    try:
        return f"{MONTHS[int(ddate[5:7]) - 1]} {ddate[:4]}"
    except (ValueError, IndexError):
        return ddate


def column_name(ddate: str, qtrs: int) -> str:
    return f"As of {month_label(ddate)}" if qtrs == 0 else f"{qtrs * 3} months ended {month_label(ddate)}"


def line_record(line: StatementLine, sheet: str, filing: Filing, ticker: str) -> Record:
    keys = sorted(line.values, key=lambda k: (k[0], -k[1]), reverse=True)[:MAX_COLUMNS]  # newest period first
    div, units = scale_for([line.values[k] for k in keys], line.uom)
    cells = {column_name(d, q): fmt(line.values[(d, q)] / div) for d, q in keys}
    file = f"R{line.report}.htm"
    body = " | ".join(f"{k}: {v}" for k, v in cells.items())
    note = " | note: shown with a negated label on the SEC page" if line.negating else ""
    text = f"{sheet} | R{line.report} line {line.line} | {line.label} | {body} | tag: {line.tag} | units: {units} | periods: month level{note}"
    return Record(
        id=f"{filing.acc_nodash}:sheet:fsds:R{line.report}:{line.line:04d}",
        ticker=ticker,
        accession=filing.accession,
        form=filing.form,
        filed=filing.filed,
        source_type="sheet",
        section_key=f"{filing.form}:SHEET",
        item=sheet,
        text=text,
        url=f"{filing.base_url}/{file}",
        file=file,
        sheet=sheet,
        row=line.line,
        cells=json.dumps(cells),
        origin="fsds",
    )


def fsds_records(store: FsdsStore, filing: Filing, ticker: str) -> list[Record]:
    out: list[Record] = []
    for code, sheet in STATEMENTS:
        out.extend(line_record(line, sheet, filing, ticker) for line in store.statement(filing.accession, code))
    return out
