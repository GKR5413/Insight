"""Golden-set evaluation, minus the model call: load questions, build the evidence the model is
given, and score what comes back.

The evidence pack is built deterministically from the FSDS SQLite file (so the check exercises the
data layer as well as the model) and the answer is scored by rule, not by another LLM. Scoring is
deliberately lenient about *format* (a date as 2026-01-30 or "January 30, 2026"; a figure as
143,756,000,000 or "$143.756 billion") and strict about *substance* (right period, right sign,
right label, abstaining when the files cannot answer).

What this does not test: retrieval over free text (Milvus) and the narrative briefing itself.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .sources.fsds import FsdsStore

ABSTAIN = "Insufficient evidence"
STATEMENT_TITLES = {"IS": "Income statement", "BS": "Balance sheet", "CF": "Cash flow", "EQ": "Equity", "CI": "Comprehensive income"}
_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "lp", "com", "the", "de"}
_STOP = {"what", "when", "were", "does", "with", "that", "this", "from", "have", "using", "only", "these", "their", "which", "will", "than", "into", "files"}


# --------------------------------------------------------------------------- golden file
@dataclass(frozen=True)
class Golden:
    id: str
    question: str
    expected: str
    evidence: tuple[dict, ...] = ()


def load_golden(path: str | Path) -> list[Golden]:
    out: list[Golden] = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(Golden(d["id"], d["question"], d["expected_answer"], tuple(d.get("evidence") or ())))
        except (json.JSONDecodeError, KeyError) as exc:
            raise ValueError(f"{path}:{n}: not a valid golden record ({exc.__class__.__name__}: {exc})") from exc
    ids = [g.id for g in out]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate ids")
    return out


# --------------------------------------------------------------------------- evidence pack
@dataclass
class Source:
    id: str
    text: str


def load_doc_chunks(path: str | Path) -> list[str]:
    """Paragraph-ish chunks (<= ~1500 chars) of the data-set documentation. PDFs go through pdftotext."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        text = subprocess.run(["pdftotext", str(p), "-"], capture_output=True, text=True, check=True).stdout
    else:
        text = p.read_text(encoding="utf-8", errors="replace")
    chunks: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not para:
            continue
        if cur and len(cur) + len(para) > 1500:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {para}".strip()
        if len(cur) >= 400:
            chunks.append(cur)
            cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def _words(s: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]{4,}", s.casefold()) if w not in _STOP}


def pick_doc_chunks(chunks: list[str], question: str, limit: int = 3, min_overlap: int = 5) -> list[str]:
    q = _words(question)
    scored = sorted(((len(q & _words(c)), i) for i, c in enumerate(chunks)), key=lambda t: (-t[0], t[1]))
    return [chunks[i] for s, i in scored[:limit] if s >= min_overlap]


def _stem(name: str) -> str:
    toks = re.sub(r"[^\w\s]", " ", name.casefold()).split()
    while len(toks) > 1 and toks[-1] in _SUFFIXES:
        toks.pop()
    return " ".join(toks)


def find_companies(store: FsdsStore, question: str, limit: int = 3) -> list[tuple[str, str]]:
    """Companies whose name (minus Inc/Corp/...) appears as whole words in the question. Best effort."""
    q = question.casefold().replace("’", "'")
    rows = store.conn.execute("SELECT cik, name, COUNT(*) AS n FROM filings GROUP BY cik, name ORDER BY n DESC").fetchall()
    hits: list[tuple[str, str, tuple[int, int]]] = []
    for r in rows:
        stem = _stem(r["name"])
        m = re.search(rf"(?<!\w){re.escape(stem)}(?!\w)", q) if len(stem) >= 3 else None
        if m:
            hits.append((r["cik"], r["name"], m.span()))
    # "Acme Holdings" also contains "Acme": the longest name wins where one match sits inside another
    keep = [h for h in hits if not any(o is not h and o[2][0] <= h[2][0] and h[2][1] <= o[2][1] and o[2] != h[2] for o in hits)]
    return [(cik, name) for cik, name, _ in keep][:limit]


def fmt_value(v: float) -> str:
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.4f}".rstrip("0").rstrip(".")


def _filing_source(store: FsdsStore, sid: str, f, statements: tuple[str, ...]) -> Source:
    lines = [f"[{sid}] {f['name']} (CIK {f['cik']}) | Form {f['form']} | fiscal year {f['fy']}, fiscal period {f['fp']} | "
             f"period ended {f['period']} | filed {f['filed']} | accession {f['adsh']}"]
    for stmt in statements:
        rows = store.statement(f["adsh"], stmt)
        if not rows:
            continue
        lines.append(f"{STATEMENT_TITLES.get(stmt, stmt)} (EDGAR report R{rows[0].report}):")
        for r in rows:
            label = store.conn.execute("SELECT tlabel FROM tags WHERE tag = ? LIMIT 1", (r.tag,)).fetchone()
            vals = "; ".join(f"period ending {d}, qtrs={q}: {fmt_value(v)} {r.uom}" for (d, q), v in r.values.items())
            lines.append(f'- line {r.line} "{r.label}" | tag {r.tag}' + (f' ("{label[0]}")' if label and label[0] else "") + f" | {vals}")
    return Source(sid, "\n".join(lines))


def build_pack(
    store: FsdsStore, question: str, doc_chunks: list[str] | None = None, statements: tuple[str, ...] = ("IS",), max_filings: int = 6
) -> list[Source]:
    sources: list[Source] = []
    for cik, _name in find_companies(store, question):
        for f in store.conn.execute("SELECT * FROM filings WHERE cik = ? ORDER BY filed DESC, adsh DESC LIMIT ?", (cik, max_filings)):
            sources.append(_filing_source(store, f"S{len(sources) + 1}", f, statements))
    for chunk in pick_doc_chunks(doc_chunks or [], question):
        sources.append(Source(f"S{len(sources) + 1}", f"[S{len(sources) + 1}] Data set documentation: {chunk}"))
    return sources


def format_pack(sources: list[Source]) -> str:
    return "\n\n".join(s.text for s in sources) if sources else "(no sources were found for this question)"


# --------------------------------------------------------------------------- diagnostics
def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.casefold())


def evidence_in_pack(g: Golden, pack_text: str) -> list[str]:
    """Golden evidence the pack failed to contain (empty = the model had what it needed)."""
    missing: list[str] = []
    flat = _norm(pack_text)
    for ev in g.evidence:
        m = ev.get("match")
        src = ev["source"]
        if m is None:  # documentation section: the expected sentence must be in a doc chunk
            if _norm(g.expected) not in flat:
                missing.append(f"{src} ({ev.get('section', '')})")
        elif src.endswith("sub.txt"):
            if m["adsh"] not in pack_text:
                missing.append(f"{src} {m['adsh']}")
        elif src.endswith("num.txt"):
            d = f"{m['ddate'][:4]}-{m['ddate'][4:6]}-{m['ddate'][6:]}"
            if f"period ending {d}, qtrs={m['qtrs']}: {fmt_value(float(m['value']))} {m['uom']}" not in pack_text:
                missing.append(f"{src} {m['tag']} {m['ddate']} qtrs={m['qtrs']}")
        elif src.endswith("pre.txt"):
            if f'"{m["plabel"]}"' not in pack_text:
                missing.append(f"{src} {m['plabel']}")
        elif src.endswith("tag.txt"):
            if f'("{m["tlabel"]}")' not in pack_text:
                missing.append(f"{src} {m['tlabel']}")
    return missing


# --------------------------------------------------------------------------- scoring
@dataclass
class Score:
    correct: bool
    reason: str = ""


_SCALE = {"trillion": 10**12, "billion": 10**9, "bn": 10**9, "million": 10**6, "thousand": 10**3}
# A minus counts only when attached to the figure ("-$1,767", "$-1,767", "(1,767)"), so "Q2 - 111,184" stays positive.
_NUM = re.compile(
    r"(?<![\w.])(?P<neg>[-−–(])?(?:\$\s?(?P<neg2>[-−–])?)?(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<scale>trillion|billion|million|thousand|bn)?\b", re.I
)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_amounts(text: str) -> list[Decimal]:
    """Every signed figure in the text, scaled by million/billion words. '(1,767)' and '-1,767' are negative."""
    out: list[Decimal] = []
    for m in _NUM.finditer(text):
        try:
            v = Decimal(m["n"].replace(",", ""))
        except InvalidOperation:
            continue
        if m["scale"]:
            v *= _SCALE[m["scale"].casefold()]
        out.append(-v if m["neg"] or m["neg2"] else v)
    return out


def parse_dates(text: str) -> set[str]:
    """Every calendar date in the text, as YYYYMMDD."""
    found: set[str] = set()

    def add(y: int, mo: int, d: int) -> None:
        try:
            found.add(date(y, mo, d).strftime("%Y%m%d"))
        except ValueError:
            pass

    for y, mo, d in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        add(int(y), int(mo), int(d))
    for s in re.findall(r"\b(\d{8})\b", text):
        add(int(s[:4]), int(s[4:6]), int(s[6:]))
    for mo, d, y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        add(int(y), int(mo), int(d))
    month = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    for mo, d, y in re.findall(rf"\b{month}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", text, re.I):
        add(int(y), _MONTHS[mo.casefold()[:3]], int(d))
    for d, mo, y in re.findall(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{month}\s*,?\s+(\d{{4}})\b", text, re.I):
        add(int(y), _MONTHS[mo.casefold()[:3]], int(d))
    return found


def score_answer(expected: str, answer: str, abstained: bool) -> Score:
    if expected.startswith(ABSTAIN):
        ok = abstained or "insufficientevidence" in _norm(answer)
        return Score(ok, "" if ok else "should have abstained: the files cannot answer this")
    if abstained:
        return Score(False, "abstained although the evidence was available")
    if re.fullmatch(r"\d{8}", expected):
        ok = expected in parse_dates(answer)
        return Score(ok, "" if ok else f"date {expected} not found in the answer")
    m = re.fullmatch(r"(-?[\d,]+(?:\.\d+)?) USD", expected)
    if m:
        want = Decimal(m[1].replace(",", ""))
        ok = want in parse_amounts(answer)
        return Score(ok, "" if ok else f"figure {want:,} (with that sign) not found in the answer")
    e, a = _norm(expected), _norm(answer)
    if expected.rstrip().endswith("."):  # a sentence: the expected wording must appear, with some room for phrasing
        ok = e in a and len(a) <= 4 * len(e) + 40
    else:  # a label: exactly as written, because "Total net sales" is not "Net sales"
        ok = a == e
    return Score(ok, "" if ok else "answer does not match the expected text")


@dataclass
class Result:
    id: str
    question: str
    expected: str
    answer: str = ""
    abstained: bool = False
    source_ids: list[str] = field(default_factory=list)
    correct: bool = False
    cited: bool = False
    reason: str = ""
    model: str = ""
    seconds: float = 0.0
    model_errors: list[str] = field(default_factory=list)  # models that failed before one answered
    evidence_missing: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.correct and self.cited
