"""Excel workbooks dropped into data/uploads/<TICKER>/ -> row records.

Row numbers are the numbers Excel itself shows (openpyxl's `cell.row`), never a dataframe
index, so a citation like "model.xlsx · Q3 · row 12" matches what an advisor sees on screen.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from ..models import Record
from .statements import _row_text

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS_PER_SHEET = 2000


def file_digest(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _fmt(v) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.4f}".rstrip("0").rstrip(".")
    if isinstance(v, (datetime, date)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    return str(v).strip()


def _find_header(rows: list[tuple]) -> int | None:
    """Index into `rows` of the first row that looks like column headings."""
    for i, cells in enumerate(rows[:15]):
        filled = [c.value for c in cells if c.value is not None and str(c.value).strip()]
        if len(filled) >= 2 and sum(isinstance(v, str) for v in filled) / len(filled) >= 0.6:
            return i
    return None


def workbook_records(path: Path, ticker: str) -> list[Record]:
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    digest = file_digest(path)
    wb = openpyxl.load_workbook(path, data_only=True)  # cached values; formulas never executed
    out: list[Record] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(max_row=MAX_ROWS_PER_SHEET))
        h = _find_header(rows)
        if h is None:
            continue
        headers = {c.column: (str(c.value).strip() if c.value is not None else "") for c in rows[h]}
        for cells in rows[h + 1 :]:
            label_cell = next((c for c in cells if isinstance(c.value, str) and c.value.strip()), None)
            if label_cell is None:
                continue
            values = {}
            for c in cells:
                if c is label_cell:
                    continue
                fv = _fmt(c.value)
                if fv is not None:
                    col = headers.get(c.column) or get_column_letter(c.column)
                    values[col if col not in values else f"{col} ({c.column})"] = fv
            if not values:
                continue
            row = label_cell.row  # the number Excel shows
            out.append(
                Record(
                    id=f"upload-{digest}:sheet:{ws.title}:{row:05d}",
                    ticker=ticker,
                    accession=f"upload-{digest}",
                    form="XLSX",
                    filed="",
                    source_type="sheet",
                    section_key="XLSX:SHEET",
                    item=ws.title,
                    text=_row_text(ws.title, row, str(label_cell.value).strip(), values, ""),
                    url="",
                    file=path.name,
                    sheet=ws.title,
                    row=row,
                    cells=json.dumps(values),
                    origin="xlsx_upload",
                )
            )
    return out
