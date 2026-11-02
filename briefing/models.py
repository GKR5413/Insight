"""Plain data types shared across the pipeline."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

SourceType = Literal["doc", "sheet"]


@dataclass(frozen=True)
class Company:
    ticker: str
    cik: str  # no leading zeros
    name: str


@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str  # 0000320193-25-000079
    form: str
    filed: str  # YYYY-MM-DD
    report_date: str
    primary_doc: str
    items: tuple[str, ...] = ()  # 8-K item numbers, e.g. ("2.02", "9.01")

    @property
    def acc_nodash(self) -> str:
        return self.accession.replace("-", "")

    @property
    def base_url(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{self.cik}/{self.acc_nodash}"

    @property
    def doc_url(self) -> str:
        return f"{self.base_url}/{self.primary_doc}"


@dataclass
class Record:
    """One retrievable unit of evidence: a document passage or a spreadsheet row."""

    id: str
    ticker: str
    accession: str
    form: str
    filed: str
    source_type: SourceType
    section_key: str  # filter key, e.g. "10-K:7", "10-Q:II.1A", "10-K:SHEET"
    item: str  # human label: "Item 7 — MD&A…" or the sheet name
    text: str
    url: str = ""
    file: str = ""  # R4.htm, my_model.xlsx, ...
    sheet: str = ""
    row: int = 0
    cells: str = ""  # JSON {column header: value} for sheet rows
    origin: str = ""  # html | sec_statement | xlsx_upload

    def embed_text(self, company: str = "") -> str:
        """Deterministic context header + body: the cheap form of contextual retrieval."""
        if self.source_type == "sheet":
            head = f"{self.ticker} | {self.form} | filed {self.filed} | {self.sheet or self.item}"
        else:
            head = f"{self.ticker} | {self.form} | filed {self.filed} | {self.item}"
        return f"{head}\n{self.text}"

    def to_row(self) -> dict:
        return asdict(self)

    def cells_dict(self) -> dict[str, str]:
        try:
            return json.loads(self.cells) if self.cells else {}
        except json.JSONDecodeError:
            return {}

    def citation_label(self) -> str:
        """Section-level label shown in the UI."""
        if self.source_type == "doc":
            return f"{self.form} · {self.item} · filed {self.filed}"
        if self.origin == "xlsx_upload":
            return f"{self.file} · {self.sheet} · row {self.row}"
        if self.origin == "fsds":  # `line` is the data set's presentation order, so name the line too
            label = self.text.split(" | ")[2] if self.text.count(" | ") >= 2 else ""
            return f"{self.form} · {self.sheet} ({self.file}) · line {self.row}: {label} · filed {self.filed}"
        return f"{self.form} · {self.sheet} ({self.file}) · row {self.row} · filed {self.filed}"


@dataclass
class Artifact:
    """A fetchable thing that yields Records: an HTML filing, an SEC statement set, an xlsx upload."""

    kind: Literal["html", "statements", "fsds", "xlsx", "pdf", "exhibit"]
    ticker: str
    filing: Filing | None
    url: str = ""
    path: str = ""  # local file for uploads
    label: str = ""

    @property
    def key(self) -> str:
        acc = self.filing.accession if self.filing else self.path
        return f"{acc}:{self.kind}:{self.url or self.path}"


@dataclass
class Quote:
    symbol: str
    price: float
    change: float | None = None
    change_pct: float | None = None
    previous_close: float | None = None
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    volume: int | None = None
    market_cap: float | None = None
    currency: str = "USD"
    as_of: str = ""  # last-trade time if the provider gives it, else retrieval time (UTC)
    source: str = ""
    delayed: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IngestReport:
    ingested: int = 0
    skipped: int = 0
    records: int = 0
    warnings: list[str] = field(default_factory=list)
