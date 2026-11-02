"""Citation verification: the gate between the model's draft and what an advisor sees.

For a document citation the quote must appear in the cited passage. For a sheet citation every
number in the quote must equal a cell in the cited row. Anything that fails is retried once,
then dropped.

Limit worth knowing: this proves the quoted text/number really exists in the cited source. It
does not prove the claim is *entailed* by the quote. That needs an entailment check or human
review, and is on the roadmap (see README).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .models import Record
from .parsing.statements import parse_number

NUM_TOKEN = re.compile(r"\(?-?\$?\s?\d[\d,]*(?:\.\d+)?\)?%?")
MIN_DOC_QUOTE = 20


def norm(s: str) -> str:
    """Whitespace-free, case-folded, punctuation-normalised form used only for comparison."""
    s = unicodedata.normalize("NFKC", s)
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-"), ("−", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", "", s).casefold()


@dataclass
class Check:
    ok: bool
    reason: str = ""


def _numbers(text: str) -> list[float]:
    out = []
    for tok in NUM_TOKEN.findall(text):
        v = parse_number(tok)
        if v is not None:
            out.append(v)
    return out


def verify_doc_quote(source_text: str, quote: str) -> Check:
    fragments = [f for f in re.split(r"…|\.\.\.", quote) if norm(f)]
    if not fragments:
        return Check(False, "empty quote")
    hay = norm(source_text)
    longest = max(len(norm(f)) for f in fragments)
    if longest < MIN_DOC_QUOTE:
        return Check(False, f"quote too short to verify (needs >= {MIN_DOC_QUOTE} characters)")
    for f in fragments:
        n = norm(f)
        if len(n) >= 12 and n not in hay:
            return Check(False, f'quote not found in cited passage: "{f.strip()[:80]}"')
    return Check(True)


def verify_sheet_quote(rec: Record, quote: str) -> Check:
    cells = rec.cells_dict()
    cell_values = [parse_number(v) for v in cells.values()]
    cell_numbers = [v for v in cell_values if v is not None]
    # numbers that merely restate the row's label or column headings (e.g. "2026", "27") are not claims
    context = " ".join([rec.text.split(" | ")[2] if rec.text.count(" | ") >= 2 else "", *cells.keys()])
    explained = set(_numbers(context))
    claimed = [v for v in _numbers(quote) if v not in explained]
    if not claimed:
        return Check(False, "quote contains no figure from the cited row")
    for v in claimed:
        if not any(abs(v - c) < 1e-9 for c in cell_numbers):
            return Check(False, f"figure {v:g} does not appear in row {rec.row} of {rec.sheet}")
    return Check(True)


def verify_citation(rec: Record, quote: str) -> Check:
    if rec.source_type == "sheet":
        return verify_sheet_quote(rec, quote)
    return verify_doc_quote(rec.text, quote)


@dataclass
class ClaimResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def verify_claim(claim: dict, registry: dict[str, Record]) -> ClaimResult:
    reasons: list[str] = []
    if not (claim.get("text") or "").strip():
        reasons.append("empty claim text")
    cites = claim.get("citations") or []
    if not cites:
        reasons.append("claim has no citation")
    for c in cites:
        sid = c.get("source_id", "")
        rec = registry.get(sid)
        if rec is None:
            reasons.append(f"{sid or '(missing id)'} is not one of the provided sources")
            continue
        chk = verify_citation(rec, c.get("quote", ""))
        if not chk.ok:
            reasons.append(f"{sid}: {chk.reason}")
    return ClaimResult(ok=not reasons, reasons=reasons)
