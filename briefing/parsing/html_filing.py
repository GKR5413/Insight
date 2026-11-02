"""EDGAR inline-XBRL HTML -> sections -> passages.

Born-digital filings are real HTML, so we parse them directly (no OCR). The pipeline:
  1. strip hidden XBRL headers/scripts, turn data tables into atomic blocks,
  2. flatten everything else into a linear list of text blocks,
  3. segment by Item headings (the table-of-contents copy loses to the real section),
  4. cut sections into ~700-token passages; tables stay whole, with their rows.
"""
from __future__ import annotations

import re
import warnings
from collections import Counter
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, XMLParsedAsHTMLWarning

from ..models import Filing, Record

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # inline-XBRL is XHTML; lxml's HTML mode is fine

SEP = "\x1e"
BLOCK_TAGS = [
    "div", "p", "tr", "td", "th", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "section", "article", "header", "footer", "blockquote", "br", "hr",
]
NUM_CELL = re.compile(r"^\(?-?\$?\s?\(?[\d,]+(?:\.\d+)?\)?%?$")
ITEM_RE = re.compile(r"^item\s+(\d{1,2}(?:\.\d{2})?[a-c]?)\s*[\.\:\-–—]?\s*(.*)$", re.I)
PART_RE = re.compile(r"^part\s+(iv|i{1,3})\b\s*[\.\:\-–—]?\s*(.*)$", re.I)
SIGNATURE_RE = re.compile(r"^signatures?$", re.I)

# Items worth embedding. 10-K Items are unique across Parts; 10-Q keys are "<Part>.<Item>".
WANTED: dict[str, set[str]] = {
    "10-K": {"1", "1A", "3", "7", "7A"},
    "10-Q": {"I.2", "I.3", "II.1", "II.1A"},
}
SKIP_8K_ITEMS = {"9.01"}  # exhibit list only
MAX_EXHIBIT_PASSAGES = 15  # the release's opening carries the news; supplements are mostly tables and cost embedding quota


@dataclass
class Block:
    text: str
    is_table: bool = False


@dataclass
class Section:
    key: str
    label: str
    order: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def _table_text(table) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [c for c in (_clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])) if c]
        merged: list[str] = []
        for c in cells:
            if c in (")", "%", ")%") and merged:
                merged[-1] += c
            elif merged and merged[-1] in ("$", "(", "($"):
                merged[-1] += c
            else:
                merged.append(c)
        if merged:
            rows.append(" | ".join(merged))
    return "\n".join(rows)


def _is_data_table(table) -> bool:
    if table.find("table"):  # wrapper/layout table
        return False
    cells = [_clean(c.get_text(" ", strip=True)) for c in table.find_all(["td", "th"])]
    merged = [c for c in cells if c and c not in ("$", ")", "%", "(")]
    return sum(1 for c in merged if NUM_CELL.match(c)) >= 3 and len(table.find_all("tr")) >= 2


def html_to_blocks(html: str) -> list[Block]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all(["script", "style", "head", "ix:header"]):
        t.decompose()
    for t in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        t.decompose()

    tables: dict[str, str] = {}
    for i, table in enumerate(soup.find_all("table")):
        if table.parent is None or not _is_data_table(table):
            continue
        key = f"@@T{i}@@"
        tables[key] = _table_text(table)
        table.replace_with(NavigableString(f"{SEP}{key}{SEP}"))

    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before(NavigableString(SEP))
        tag.insert_after(NavigableString(SEP))

    blocks: list[Block] = []
    for part in soup.get_text(" ").split(SEP):
        s = _clean(part)
        if not s:
            continue
        if s in tables:
            if tables[s]:
                blocks.append(Block(tables[s], True))
        else:
            blocks.append(Block(s))
    return _drop_boilerplate(blocks)


def _is_heading_text(t: str) -> bool:
    return bool(ITEM_RE.match(t) or PART_RE.match(t))


def _drop_boilerplate(blocks: list[Block]) -> list[Block]:
    counts = Counter(b.text for b in blocks if not b.is_table and len(b.text) < 100)
    out = []
    for b in blocks:
        if not b.is_table:
            if re.fullmatch(r"\d{1,3}", b.text) or re.fullmatch(r"(page )?\d+ of \d+", b.text, re.I):
                continue
            if counts[b.text] >= 6 and not _is_heading_text(b.text):  # running headers/footers
                continue
        out.append(b)
    return out


def _plausible_title(t: str) -> bool:
    if not t:
        return True
    if re.match(r"(of|in|to|and|or|for|above|below|under|is|are)\b", t, re.I):
        return False  # a sentence that merely starts with "Item 1A ..."
    return t[0].isupper() or t[0] in "“\"'"


def segment(blocks: list[Block], form: str) -> list[Section]:
    sections: list[Section] = []
    cur: Section | None = None
    part = ""
    i = 0
    while i < len(blocks):
        b = blocks[i]
        i += 1
        if b.is_table:
            if cur:
                cur.blocks.append(b)
            continue
        if SIGNATURE_RE.match(b.text):
            cur = None
            continue
        pm = PART_RE.match(b.text) if len(b.text) <= 120 else None
        if pm and form.startswith("10-"):
            part = pm.group(1).upper()
            cur = None
            continue
        m = ITEM_RE.match(b.text) if len(b.text) <= 220 else None
        if m and _plausible_title(m.group(2).strip()):
            num = m.group(1).upper()
            title = m.group(2).strip(" .:-–—")
            if not title and i < len(blocks):
                nb = blocks[i]
                if not nb.is_table and len(nb.text) <= 120 and not _is_heading_text(nb.text):
                    title = nb.text.strip(" .:-–—")
                    i += 1
            if form.startswith("10-Q"):
                key, label = f"{part}.{num}", f"Part {part}, Item {num}"
            else:
                key, label = num, f"Item {num}"
            if title:
                label += f" — {title}"
            cur = Section(key=key, label=label, order=len(sections))
            sections.append(cur)
            continue
        if cur:
            cur.blocks.append(b)

    # The table of contents repeats every heading with almost no body: keep the biggest copy.
    best: dict[str, Section] = {}
    for s in sections:
        if s.key not in best or s.chars > best[s.key].chars:
            best[s.key] = s
    return sorted(best.values(), key=lambda s: s.order)


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars * 1.4:
        return [text]
    out, buf = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if buf and len(buf) + len(sent) > max_chars:
            out.append(buf)
            buf = ""
        buf = f"{buf} {sent}".strip()
    if buf:
        out.append(buf)
    return out


def _split_table(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    lines = text.split("\n")
    head, out, buf = lines[0], [], []
    size = 0
    for ln in lines:
        if buf and size + len(ln) > max_chars:
            out.append("\n".join(buf))
            buf, size = [head], len(head)
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        out.append("\n".join(buf))
    return out


def section_passages(section: Section, max_chars: int = 2800) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    size = 0

    def flush():
        nonlocal buf, size
        if buf:
            out.append("\n".join(buf))
        buf, size = [], 0

    for b in section.blocks:
        if b.is_table:
            flush()
            out.extend(f"[Table]\n{t}" for t in _split_table(b.text, 6000))
            continue
        for piece in _split_long(b.text, max_chars):
            if buf and size + len(piece) > max_chars:
                flush()
            buf.append(piece)
            size += len(piece) + 1
    flush()
    return [p for p in out if len(p) >= 40]


def parse_filing_html(html: str, form: str) -> list[Section]:
    """Sections worth embedding for this form."""
    sections = segment(html_to_blocks(html), form)
    if form in WANTED:
        sections = [s for s in sections if s.key in WANTED[form]]
    elif form == "8-K":
        sections = [s for s in sections if s.key not in SKIP_8K_ITEMS]
    return [s for s in sections if s.chars >= 200]


def sections_to_records(sections: list[Section], filing: Filing, ticker: str, url: str, origin: str = "html") -> list[Record]:
    out: list[Record] = []
    for s in sections:
        for n, text in enumerate(section_passages(s)):
            out.append(
                Record(
                    id=f"{filing.acc_nodash}:doc:{s.key}:{n:03d}",
                    ticker=ticker,
                    accession=filing.accession,
                    form=filing.form,
                    filed=filing.filed,
                    source_type="doc",
                    section_key=f"{filing.form}:{s.key}",
                    item=s.label,
                    text=text,
                    url=url,
                    origin=origin,
                )
            )
    return out


def parse_exhibit_html(html: str, filing: Filing, ticker: str, url: str, name: str) -> list[Record]:
    """Exhibits (e.g. EX-99.1 earnings releases) have no Item structure: one section."""
    blocks = html_to_blocks(html)
    num = "99.1" if "991" in name.replace("-", "").replace("_", "") else "99"
    sec = Section(key=f"EX{num}", label=f"Exhibit {num} — press release / supplemental information", order=0, blocks=blocks)
    if sec.chars < 200:
        return []
    return sections_to_records([sec], filing, ticker, url, origin="exhibit")[:MAX_EXHIBIT_PASSAGES]
