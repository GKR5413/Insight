"""Tabular evidence: SEC rendered statements (R2.htm, R4.htm, ...) and Excel sheets share one row model.

Every record is a single row with its row number, so a claim can be cited as
"<sheet> · row N" and checked cell-for-cell. (EDGAR's Financial_Report.xlsx is not reliably
published for new filings, so the R pages, which carry the same statement tables, are the
dependable source.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..models import Filing, Record
from .html_filing import NUM_CELL, _clean


@dataclass
class SheetRow:
    row: int
    label: str
    cells: dict[str, str]
    text: str


def _grid(table) -> list[tuple[int, list[str]]]:
    """(row number, cells) with colspan AND rowspan expanded, so columns line up.

    SEC statement headers use rowspan on the title cell; ignoring it shifts every date one
    column to the left.
    """
    rows: list[tuple[int, list[str]]] = []
    carry: dict[int, list] = {}  # column -> [rows still covered, text]
    for n, tr in enumerate(table.find_all("tr"), 1):
        cells: list[str] = []

        def fill_carried():
            while len(cells) in carry:
                entry = carry[len(cells)]
                cells.append(entry[1])
                entry[0] -= 1
                if entry[0] <= 0:
                    del carry[len(cells) - 1]

        for c in tr.find_all(["th", "td"]):
            fill_carried()
            colspan = max(1, int(c.get("colspan", 1) or 1))
            rowspan = max(1, int(c.get("rowspan", 1) or 1))
            text = _clean(c.get_text(" ", strip=True))
            for _ in range(colspan):
                if rowspan > 1:
                    carry[len(cells)] = [rowspan - 1, text]
                cells.append(text)
        fill_carried()
        rows.append((n, cells))
    return rows


def parse_statement_html(html: str, sheet_name: str) -> tuple[str, list[SheetRow]]:
    """Returns (units string, rows). Row numbers are 1-based positions in the rendered table."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="report") or soup.find("table")
    if table is None:
        return "", []
    expanded = _grid(table)

    header: list[list[str]] = []
    idx = 0
    while idx < min(3, len(expanded)):
        cells = expanded[idx][1]
        vals = [c for c in cells[1:] if c]
        if vals and not any(NUM_CELL.match(v) for v in vals):
            header.append(cells)
            idx += 1
        else:
            break

    title = header[0][0] if header and header[0] else sheet_name
    units = title.split(" - ", 1)[1].strip() if " - " in title else ""
    width = max((len(c) for _, c in expanded), default=0)
    cols: list[str] = []
    for j in range(1, width):
        parts: list[str] = []
        for h in header:
            if j < len(h) and h[j] and h[j] not in parts:
                parts.append(h[j])
        name = " ".join(parts) or f"col{j}"
        n, base = 2, name
        while name in cols:  # duplicate headers (e.g. equity members) stay distinguishable
            name, n = f"{base} ({n})", n + 1
        cols.append(name)

    rows: list[SheetRow] = []
    for n, cells in expanded[idx:]:
        if not cells or not cells[0] or "[Abstract]" in cells[0]:
            continue
        values = {cols[j - 1]: v for j, v in enumerate(cells) if j >= 1 and v and j - 1 < len(cols)}
        if not values:
            continue
        rows.append(SheetRow(row=n, label=cells[0], cells=values, text=_row_text(sheet_name, n, cells[0], values, units)))
    return units, rows


def _row_text(sheet: str, row: int, label: str, values: dict[str, str], units: str) -> str:
    body = " | ".join(f"{k}: {v}" for k, v in values.items())
    return f"{sheet} | row {row} | {label} | {body}" + (f" | units: {units}" if units else "")


def statement_records(rows: list[SheetRow], filing: Filing, ticker: str, sheet: str, file: str, url: str) -> list[Record]:
    return [
        Record(
            id=f"{filing.acc_nodash}:sheet:{file}:{r.row:04d}",
            ticker=ticker,
            accession=filing.accession,
            form=filing.form,
            filed=filing.filed,
            source_type="sheet",
            section_key=f"{filing.form}:SHEET",
            item=sheet,
            text=r.text,
            url=url,
            file=file,
            sheet=sheet,
            row=r.row,
            cells=json.dumps(r.cells),
            origin="sec_statement",
        )
        for r in rows
    ]


def parse_number(s: str) -> float | None:
    """'$ (1,234.5)' -> -1234.5 ; '12%' -> 12.0 ; non-numeric -> None."""
    t = s.strip().replace("$", "").replace(",", "").replace(" ", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").rstrip("%")
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v
