"""Pre-deploy check: run the exact golden questions through the configured Gemini model(s) and score them.

    python scripts/eval_golden.py                    # all questions, needs GOOGLE_API_KEY and data/fsds.sqlite
    python scripts/eval_golden.py --dry-run          # no model call: only checks the model would be GIVEN the evidence
    python scripts/eval_golden.py --only SEC-06 SEC-13
    python scripts/eval_golden.py --show-pack SEC-13 # print the sources the model sees for one question

Exit code: 0 when the pass rate reaches --min-pass (default 100%), so it can gate a deploy; 1 when answers were
wrong; 2 when the check could not complete because models errored (quota 429, overload 503): that is
"not evaluated", not "failed".
Models are tried in the order of LLM_MODEL then LLM_FALLBACK_MODELS, as the app does.

The questions are answered from an evidence pack built out of data/fsds.sqlite plus the data-set
documentation PDF. This checks the model and the data layer; it does not test Milvus retrieval or
the narrative briefing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field  # noqa: E402

from briefing.config import ROOT, get_settings  # noqa: E402
from briefing.golden import ABSTAIN, Result, build_pack, evidence_in_pack, format_pack, load_doc_chunks, load_golden, score_answer  # noqa: E402
from briefing.sources.fsds import FsdsStore  # noqa: E402

GOLDEN_DIR = ROOT / "tests" / "golden"

SYSTEM = f"""You answer questions about SEC filings using ONLY the numbered sources provided. Never use outside knowledge.

- Reply with just the answer in the form asked: a date (YYYY-MM-DD), a figure with its unit written out in full (e.g. 143,756,000,000 USD, no abbreviations or conversions), a label exactly as written, or one short sentence.
- A change between two periods is a signed figure: later minus earlier, keeping the minus sign when it fell.
- Statement figures carry qtrs: qtrs=1 is a single quarter, 2 is six months year-to-date, 3 is nine months, 4 is a full year, 0 is a point in time. "For the quarter alone" means qtrs=1.
- "Fiscal Q1 2026" is the filing whose fiscal year is 2026 and fiscal period is Q1, as stated in each source header.
- If the sources do not contain the answer (for example a period that has not been reported), set insufficient_evidence to true and answer "{ABSTAIN}." Do not estimate or extrapolate.
- List the ids of the sources you used in source_ids."""


class GoldenAnswer(BaseModel):
    answer: str = Field(description="The answer only, in the form asked.")
    insufficient_evidence: bool = Field(description="True when the sources cannot answer the question.")
    source_ids: list[str] = Field(default_factory=list, description="Aliases of the sources used, e.g. ['S1'].")


def build_chains(models: list[str], api_key: str):
    from langchain_google_genai import ChatGoogleGenerativeAI

    # Same convention as the app: Gemini 3.x fixes its sampling parameters.
    return [
        (m, ChatGoogleGenerativeAI(model=m, google_api_key=api_key, max_retries=1, timeout=90.0, **({} if m.startswith("gemini-3") else {"temperature": 0})).with_structured_output(GoldenAnswer))
        for m in models
    ]


def ask(chains, question: str, pack_text: str) -> tuple[GoldenAnswer, str, list[str]]:
    errors: list[str] = []
    for model, chain in chains:
        try:
            out = chain.invoke([("system", SYSTEM), ("human", f"Question: {question}\n\nSources:\n{pack_text}")])
            if out is None:
                raise RuntimeError("returned no structured output")
            return (out if isinstance(out, GoldenAnswer) else GoldenAnswer.model_validate(out)), model, errors
        except Exception as exc:  # noqa: BLE001 - try the next model; report everything if all fail
            errors.append(f"{model}: {exc.__class__.__name__}: {str(exc)[:120]}")
    raise RuntimeError(" | ".join(errors))


def main() -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", type=Path, default=GOLDEN_DIR / "sec-golden.jsonl")
    ap.add_argument("--doc", type=Path, default=GOLDEN_DIR / "financial-statement-data-sets.pdf", help="data-set documentation (pdf/txt)")
    ap.add_argument("--db", type=Path, default=s.fsds_db)
    ap.add_argument("--statements", default="IS", help="statements to include per filing, e.g. IS,BS,CF")
    ap.add_argument("--only", nargs="*", help="question ids to run")
    ap.add_argument("--dry-run", action="store_true", help="build evidence only; no model calls")
    ap.add_argument("--show-pack", metavar="ID", help="print the sources for one question and exit")
    ap.add_argument("--min-pass", type=float, default=1.0, help="required pass rate (default 1.0)")
    ap.add_argument("--out", type=Path, help="report path (default: data/eval/golden-<timestamp>.json)")
    args = ap.parse_args()

    golden = load_golden(args.golden)
    if args.only:
        golden = [g for g in golden if g.id in set(args.only)]
    if args.show_pack:
        golden = [g for g in load_golden(args.golden) if g.id == args.show_pack]
    if not golden:
        print("No matching questions.")
        return 1
    try:
        store = FsdsStore(args.db)
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot open {args.db}: {exc}")
        return 1
    try:
        chunks = load_doc_chunks(args.doc)
    except Exception as exc:  # noqa: BLE001 - pdftotext missing or file absent
        print(f"warning: documentation not loaded ({exc.__class__.__name__}); documentation questions cannot pass")
        chunks = []
    statements = tuple(x.strip().upper() for x in args.statements.split(",") if x.strip())

    if args.show_pack:
        print(format_pack(build_pack(store, golden[0].question, chunks, statements)))
        return 0

    chains = None
    if not args.dry_run:
        if not s.google_api_key:
            print("GOOGLE_API_KEY is not set. Put it in .env, or use --dry-run.")
            return 1
        models = [s.llm_model, *[m.strip() for m in s.llm_fallback_models.split(",") if m.strip() and m.strip() != s.llm_model]]
        chains = build_chains(models, s.google_api_key)
        print(f"models (in order): {', '.join(models)}")

    results: list[Result] = []
    for g in golden:
        pack = build_pack(store, g.question, chunks, statements)
        pack_text = format_pack(pack)
        r = Result(g.id, g.question, g.expected, evidence_missing=evidence_in_pack(g, pack_text))
        if chains is not None:
            t0 = time.time()
            try:
                out, r.model, r.model_errors = ask(chains, g.question, pack_text)
                r.answer, r.abstained, r.source_ids = out.answer.strip(), out.insufficient_evidence, out.source_ids
                known = {x.id for x in pack}
                r.cited = r.abstained or any(i in known for i in r.source_ids)
                sc = score_answer(g.expected, r.answer, r.abstained)
                r.correct = sc.correct
                r.reason = sc.reason or ("" if r.cited else "correct but cites no provided source")
            except Exception as exc:  # noqa: BLE001 - a model outage is a failed check, not a crash
                r.reason = f"model error: {exc}"
                r.model_errors = str(exc).split(" | ")
            r.seconds = time.time() - t0
        results.append(r)
        _print(r, dry=args.dry_run)

    n_ev = sum(1 for r in results if not r.evidence_missing)
    print(f"\nevidence present in what the model is given: {n_ev}/{len(results)}")
    if args.dry_run:
        return 0 if n_ev == len(results) else 1
    passed = sum(r.passed for r in results)
    rate = passed / len(results)
    failures: dict[str, int] = {}
    for r in results:
        for e in r.model_errors:
            failures[e[:110]] = failures.get(e[:110], 0) + 1
    for e, n in sorted(failures.items(), key=lambda kv: -kv[1]):
        print(f"fell back past: {n}x {e}")
    print(f"passed: {passed}/{len(results)} ({rate:.0%}) | required: {args.min_pass:.0%} | models used: {sorted({r.model for r in results if r.model})}")
    out = args.out or s.data_dir / "eval" / f"golden-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ran_at": datetime.now().isoformat(timespec="seconds"), "pass_rate": rate, "results": [r.__dict__ for r in results]}, indent=2))
    print(f"report: {out}")
    if rate >= args.min_pass:
        return 0
    errored = [r.id for r in results if r.reason.startswith("model error")]
    if errored:
        print(f"NOT EVALUATED: {len(errored)} question(s) got no model answer ({', '.join(errored)}). Retry when quota/availability recovers.")
        return 2
    return 1


def _print(r: Result, *, dry: bool) -> None:
    if dry:
        print(f"{'ok  ' if not r.evidence_missing else 'MISS'} {r.id}  evidence in pack" + (f": missing {r.evidence_missing}" if r.evidence_missing else ""))
        return
    print(f"{'PASS' if r.passed else 'FAIL'} {r.id}  {r.seconds:4.1f}s  {r.model:<24} {r.answer[:90]!r}")
    if not r.passed:
        print(f"       expected: {r.expected!r}\n       why: {r.reason}" + (f"\n       evidence missing from pack: {r.evidence_missing}" if r.evidence_missing else ""))


if __name__ == "__main__":
    raise SystemExit(main())
