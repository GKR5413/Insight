"""Quality tracking: one record per brief, scores attached to LangSmith runs, and a report.

What is tracked (and why each one matters to an advisor-facing product):
  first_pass_rate   share of the model's first-draft claims whose quotes verified. The honest model
                    quality number: the retry and the drop hide misses from the user, not from us.
  removed           claims the user never saw because they could not be verified even after a retry.
  complete          False when a filing failed to index, so the brief rests on thinner evidence.
  llm_used          which model answered, since the fallback chain means it is not always the primary.
  error_kind        why a run failed (quota, overload, no evidence, config), to separate "the model is
                    bad" from "the free tier is exhausted".

Records go to data/quality/briefs.jsonl always. When LangSmith tracing is on, the same scores are
attached to the run as feedback, so they can be sliced by tag/metadata (ticker, model, prompt version)
in the LangSmith UI. Nothing here ever raises into the request path.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ERROR_KINDS = (
    ("embedding_quota", ("Embedding quota used up", "quota or rate limit")),
    ("llm_unavailable", ("Gemini could not answer",)),
    ("no_evidence", ("No evidence could be retrieved",)),
    ("unknown_ticker", ("No SEC registrant", "not a valid ticker")),
    ("config", ("Cannot open Milvus", "is not set", "SEC_USER_AGENT")),
)


def classify_error(message: str) -> str:
    for kind, needles in ERROR_KINDS:
        if any(n in message for n in needles):
            return kind
    return "other"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def brief_metrics(ticker: str, final: dict, brief: dict, duration: float, run_id: str = "", source: str = "live") -> dict:
    meta = brief.get("meta", {})
    drafted = final.get("claims_drafted")
    failed = meta.get("first_pass_failed", 0)
    final_claims = sum(len(s["claims"]) for s in brief.get("sections", []))
    return {
        "ts": now_iso(), "source": source, "run_id": run_id, "ticker": ticker, "outcome": "ok",
        "duration_s": round(duration, 1) if duration is not None else None,
        "llm_used": meta.get("llm"), "claims_drafted": drafted,
        "first_pass_failed": failed,
        "first_pass_rate": round(1 - failed / drafted, 3) if drafted else None,
        "retries": meta.get("retries", 0), "removed": brief.get("removed_claims", 0), "final_claims": final_claims,
        "sources_cited": len(brief.get("sources", [])), "evidence_items": meta.get("evidence_items"),
        "complete": bool(meta.get("complete", True)), "figure_sources": meta.get("figure_sources", []),
        "warnings": len(brief.get("warnings", [])),
    }


def error_metrics(ticker: str, message: str, duration: float, run_id: str = "", source: str = "live") -> dict:
    return {"ts": now_iso(), "source": source, "run_id": run_id, "ticker": ticker, "outcome": "error",
            "error_kind": classify_error(message), "error": message[:200], "duration_s": round(duration, 1)}


def cache_hit_metrics(ticker: str, run_id: str = "") -> dict:
    return {"ts": now_iso(), "source": "live", "run_id": run_id, "ticker": ticker, "outcome": "cache_hit"}


def append(log_dir: Path, record: dict) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "briefs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        log.warning("could not write quality log: %s", exc)


def load(log_dir: Path) -> list[dict]:
    path = Path(log_dir) / "briefs.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ------------------------------------------------------------------ LangSmith
FEEDBACK_KEYS = ("first_pass_rate", "removed", "final_claims", "complete", "retried")


def feedback_scores(m: dict) -> dict[str, float]:
    s: dict[str, float] = {"complete": 1.0 if m.get("complete") else 0.0, "retried": 1.0 if m.get("retries") else 0.0,
                           "removed": float(m.get("removed", 0)), "final_claims": float(m.get("final_claims", 0))}
    if m.get("first_pass_rate") is not None:
        s["first_pass_rate"] = float(m["first_pass_rate"])
    return s


def push_feedback(client, run_id: str, m: dict) -> int:
    """Attach scores to the LangSmith run. Returns how many were sent; never raises."""
    sent = 0
    try:
        for key, score in feedback_scores(m).items():
            client.create_feedback(run_id=run_id, key=key, score=score, source_info={"origin": "stock-briefing quality gate"})
            sent += 1
    except Exception as exc:  # noqa: BLE001 - observability must not break a brief
        log.warning("LangSmith feedback failed: %s", exc.__class__.__name__)
    return sent


def langsmith_enabled() -> bool:
    try:
        from langsmith.utils import tracing_is_enabled

        return bool(tracing_is_enabled())
    except Exception:  # noqa: BLE001
        return False


def record_run(log_dir: Path, record: dict, *, client=None) -> None:
    append(log_dir, record)
    if record.get("outcome") == "ok" and record.get("run_id") and langsmith_enabled():
        try:
            if client is None:
                from langchain_core.tracers.langchain import wait_for_all_tracers
                from langsmith import Client

                wait_for_all_tracers()  # the run must exist in LangSmith before feedback can attach to it
                client = Client()
            push_feedback(client, record["run_id"], record)
        except Exception as exc:  # noqa: BLE001
            log.warning("LangSmith feedback skipped: %s", exc.__class__.__name__)


# ------------------------------------------------------------------ report
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _agg(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("outcome") == "ok"]
    drafted = sum(r.get("claims_drafted") or 0 for r in ok)
    failed = sum(r.get("first_pass_failed") or 0 for r in ok if r.get("claims_drafted"))
    durations = [r["duration_s"] for r in ok if r.get("duration_s")]
    return {
        "runs": len(rows), "ok": len(ok), "errors": sum(r.get("outcome") == "error" for r in rows),
        "drafted": drafted, "first_pass_rate": (1 - failed / drafted) if drafted else None,
        "removed": sum(r.get("removed", 0) for r in ok), "final": sum(r.get("final_claims", 0) for r in ok),
        "complete_rate": (sum(1 for r in ok if r.get("complete")) / len(ok)) if ok else None,
        "median_s": statistics.median(durations) if durations else None,
    }


def render_report(records: list[dict], golden: list[dict], langsmith: dict | None, generated: str | None = None) -> str:
    fresh = [r for r in records if r.get("outcome") in ("ok", "error")]
    hits = [r for r in records if r.get("outcome") == "cache_hit"]
    a = _agg(fresh)
    L: list[str] = [f"# Quality tracking report", f"", f"Generated {generated or now_iso()}. "
                    f"{len(fresh)} fresh brief attempt(s) logged, {len(hits)} served from cache."]

    L += ["", "## 1. Tracking status", ""]
    if langsmith is None:
        L.append("- **LangSmith:** not queried (run with `--langsmith` to include it).")
    elif not langsmith.get("project_found"):
        L.append(f"- **LangSmith:** the key works, but project `{langsmith.get('project')}` does not exist, so **no traces have been sent** "
                 "(tracing is off). Scores below come from the local quality log. Enable tracing to get per-run traces, "
                 "latency, token cost and the same scores as feedback (see section 6).")
    else:
        L.append(f"- **LangSmith:** project `{langsmith['project']}` has {langsmith['runs']} root run(s); "
                 f"error rate {_pct(langsmith['error_rate'])}, median latency {langsmith['median_s'] or 'n/a'}s, "
                 f"tokens {langsmith['tokens']}, cost ${langsmith['cost']:.4f}.")
        for k, v in sorted(langsmith.get("feedback", {}).items()):
            L.append(f"  - feedback `{k}`: n={v['n']}, mean={v['avg']:.3f}")
    L.append("- **Local log:** `data/quality/briefs.jsonl` (one line per brief; written on every run).")

    L += ["", "## 2. Headline", ""]
    if not a["ok"]:
        L.append("No successful brief has been logged yet.")
    else:
        L += [
            "| Measure | Value | What it means |", "|---|---|---|",
            f"| Brief attempts | {a['runs']} ({a['ok']} ok, {a['errors']} failed) | |",
            f"| **First-pass verification rate** | **{_pct(a['first_pass_rate'])}** of {a['drafted']} drafted claims | model quality before retry/drop hides misses |",
            f"| Claims removed after retry | {a['removed']} of {a['final'] + a['removed']} delivered-or-removed | claims the user never saw |",
            f"| Complete briefs | {_pct(a['complete_rate'])} | incomplete = a filing failed to index |",
            f"| Median build time | {a['median_s']}s | includes quota pacing on a cold ticker |",
        ]
        L.append(f"\nSample size is {a['ok']} successful brief(s). Treat every rate as anecdotal until there are dozens.")

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in fresh:
        if r.get("outcome") == "ok":
            by_model[r.get("llm_used") or "unknown"].append(r)
    if by_model:
        L += ["", "## 3. By model (the fallback chain means the answering model varies)", "",
              "| Model | Briefs | Claims drafted | First-pass rate | Removed | Complete |", "|---|---|---|---|---|---|"]
        for model, rows in sorted(by_model.items()):
            g = _agg(rows)
            L.append(f"| `{model}` | {g['ok']} | {g['drafted']} | {_pct(g['first_pass_rate'])} | {g['removed']} | {_pct(g['complete_rate'])} |")

    errs: dict[str, int] = defaultdict(int)
    for r in fresh:
        if r.get("outcome") == "error":
            errs[r.get("error_kind", "other")] += 1
    if errs:
        L += ["", "## 4. Failures by cause", "", "| Cause | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(errs.items(), key=lambda kv: -kv[1])]
        L.append("\n`embedding_quota` and `llm_unavailable` are capacity problems (free tier / provider load), not answer-quality problems.")

    if fresh:
        L += ["", "## 5. Every run", "", "| When (UTC) | Ticker | Outcome | Model | Drafted | First pass | Retry | Removed | Complete | Secs | Source |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(fresh, key=lambda r: r["ts"]):
            if r["outcome"] == "ok":
                L.append(f"| {r['ts'][5:16]} | {r['ticker']} | ok | `{r.get('llm_used')}` | {r.get('claims_drafted')} | {_pct(r.get('first_pass_rate'))} | {r.get('retries')} | {r.get('removed')} | {'yes' if r.get('complete') else '**no**'} | {r.get('duration_s') or '–'} | {r.get('source')} |")
            else:
                L.append(f"| {r['ts'][5:16]} | {r['ticker']} | **error: {r.get('error_kind')}** | – | – | – | – | – | – | {r.get('duration_s') or '–'} | {r.get('source')} |")

    L += ["", "## 6. Golden-question evaluation (model + data layer)", ""]
    if not golden:
        L.append("No golden eval report found in `data/eval/`. Run `python scripts/eval_golden.py` (15 questions, rule-scored).")
    else:
        latest = golden[-1]
        res = latest.get("results", [])
        answered = [r for r in res if r.get("model")]
        passed = sum(1 for r in res if r.get("correct") and r.get("cited", True))
        L.append(f"Latest run {latest.get('ran_at')}: **{passed} passed of {len(res)} questions run** "
                 f"({len(answered)} got a model answer; {len(res) - len(answered)} got none because of model errors). The golden file has 15 questions in total.")
        if len(res) < 15:
            L.append("\n**This is not a quality measurement yet:** most questions were never run. Re-run when quota/availability allows; the script gates at 100% by default.")

    L += ["", "## 7. Turning on LangSmith tracking", "",
          "1. In `.env` set `LANGSMITH_TRACING=true` (the key is already there). Every brief then appears as a `stock_brief` trace "
          "tagged `ticker:<SYM>` with metadata `llm`, `embedding`, `prompt_version`.",
          "2. The five scores (`first_pass_rate`, `removed`, `final_claims`, `complete`, `retried`) are attached to each trace automatically; "
          "filter or chart them by the metadata above in the LangSmith UI.",
          "3. **What leaves your machine when tracing is on:** the prompts (public SEC filing text and the facet instructions), the model's "
          "claims, and timings/token counts, sent to your LangSmith workspace. No keys or client data are included; keep it that way if "
          "portfolio context is ever added (redact first).",
          "4. Re-run `python scripts/quality_report.py --langsmith` to merge trace latency, tokens and cost into this report."]
    return "\n".join(L) + "\n"


def langsmith_summary(project: str, api_key: str) -> dict:
    """Aggregate root runs of the project; {'project_found': False} when nothing was ever traced."""
    from langsmith import Client

    c = Client(api_key=api_key)
    names = {p.name for p in c.list_projects(limit=100)}
    if project not in names:
        return {"project": project, "project_found": False}
    runs = list(c.list_runs(project_name=project, is_root=True, limit=500))
    lat = [(r.end_time - r.start_time).total_seconds() for r in runs if r.end_time and r.start_time]
    fb: dict[str, dict] = {}
    for r in runs:
        for k, v in (getattr(r, "feedback_stats", None) or {}).items():
            n = v.get("n", 0)
            if n:
                cur = fb.setdefault(k, {"n": 0, "sum": 0.0})
                cur["n"] += n
                cur["sum"] += (v.get("avg") or 0.0) * n
    return {
        "project": project, "project_found": True, "runs": len(runs),
        "error_rate": (sum(1 for r in runs if r.status == "error") / len(runs)) if runs else None,
        "median_s": round(statistics.median(lat), 1) if lat else None,
        "tokens": sum(r.total_tokens or 0 for r in runs), "cost": float(sum(r.total_cost or 0 for r in runs)),
        "feedback": {k: {"n": v["n"], "avg": v["sum"] / v["n"]} for k, v in fb.items()},
    }
