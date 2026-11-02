"""The main graph.

    START -> resolve ─┬─> fetch_quote ───────────────────────────────────────────► END
                      └─> plan_ingest ─(Send per new artifact)─> worker ─┐
                                      └─(nothing new)───────────────────┴─> retrieve
    retrieve -> summarize -> verify ─┬─(failures & first try)─> repair ─> verify
                                     └─────────────────────────────────> finalize -> END

Quote and ingestion run in parallel. `worker` fans out one task per new filing artifact and
invokes the artifact graph (parser routing + indexing). Verification failures are retried once,
then dropped. A finished brief is cached in Redis by filing set + model versions, so repeat
lookups cost nothing and the quote is always fetched fresh.
"""
from __future__ import annotations

import hashlib
import json
import operator
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..embeddings import QuotaExhausted
from ..events import emit, progress, short_error
from ..facets import FACETS, to_record
from ..llm import PROMPT_VERSION
from ..models import Artifact
from ..parsing.xlsx_upload import file_digest
from ..services import Services
from ..sources.edgar import EdgarError
from ..verify import verify_claim
from .artifact import build_artifact_graph


class NoEvidence(RuntimeError):
    pass


class State(TypedDict, total=False):
    ticker: str
    refresh: bool
    company: Any
    filings: list
    uploads: list
    accessions: list[str]
    cache_key: str
    cache_hit: bool
    quote: dict | None
    artifacts: list
    results: Annotated[list[dict], operator.add]
    warnings: Annotated[list[str], operator.add]
    registry: dict
    by_facet: dict
    claims: list[dict]
    failures: list[dict]
    retries: int
    first_pass_failed: int
    llm_used: str
    claims_drafted: int
    brief: dict


def scan_uploads(uploads_dir: Path, ticker: str) -> list[Path]:
    d = uploads_dir / ticker
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in (".xlsx", ".pdf") and not p.name.startswith(("~$", ".")))


def build_brief_graph(svc: Services):
    artifact_graph = build_artifact_graph(svc)
    cfg = svc.settings

    # ------------------------------------------------------------------ nodes
    def resolve(state: State) -> State:
        progress("resolve", "Looking up the company and its recent filings…", "start")
        company = svc.edgar.resolve(state["ticker"])
        filings = svc.edgar.select_filings(svc.edgar.recent_filings(company.cik))
        if not filings:
            raise EdgarError(f"{company.name} has no recent 10-K, 10-Q or 8-K filings to summarize.")
        warnings = []
        forms = {f.form for f in filings}
        for need in ("10-K", "10-Q"):
            if need not in forms:
                warnings.append(f"No {need} found for {company.ticker}; the brief will not cover it.")
        uploads = scan_uploads(cfg.uploads_dir, company.ticker)
        digests = {str(p): file_digest(p) for p in uploads if p.suffix.lower() == ".xlsx"}
        accessions = [f.accession for f in filings] + [f"upload-{d}" for d in digests.values()]
        key_src = json.dumps(
            {"t": company.ticker, "a": sorted(accessions), "llm": svc.summarizer.name, "emb": [svc.embedder.model, svc.embedder.dim], "p": PROMPT_VERSION},
            sort_keys=True,
        )
        cache_key = "brief:" + hashlib.sha1(key_src.encode()).hexdigest()
        out: State = {"company": company, "filings": filings, "uploads": uploads, "accessions": accessions, "cache_key": cache_key, "warnings": warnings}
        cached = None if state.get("refresh") else svc.cache.get_json(cache_key)
        if cached:
            out["brief"] = cached
            out["cache_hit"] = True
        progress("resolve", f"{company.name}: {len(filings)} filings selected" + (" (cached brief found)" if cached else ""), "done")
        return out

    def fetch_quote(state: State) -> State:
        progress("quote", "Fetching delayed quote…", "start")
        try:
            q = svc.quotes.get(state["company"].ticker).to_dict()
        except Exception as exc:  # noqa: BLE001 - a missing quote must not sink the brief
            progress("quote", "Quote unavailable", "done")
            return {"quote": None, "warnings": [f"Quote unavailable: {short_error(exc)}"]}
        emit({"type": "quote", "quote": q})
        progress("quote", f"Quote from {q['source']}", "done")
        return {"quote": q}

    def plan_ingest(state: State) -> State:
        t = state["company"].ticker
        force = bool(state.get("refresh"))
        planned: list[Artifact] = []
        skipped = 0
        for f in state["filings"]:
            label = f"{f.form} filed {f.filed}"
            if force or not svc.store.has(f.accession, "html"):
                planned.append(Artifact("html", t, f, url=f.doc_url, label=label))
            else:
                skipped += 1
            if f.form in ("10-K", "10-Q"):
                if force or not (svc.store.has(f.accession, "fsds") or svc.store.has(f.accession, "sec_statement")):
                    in_fsds = bool(svc.fsds and svc.fsds.has_filing(f.accession))
                    planned.append(Artifact("fsds" if in_fsds else "statements", t, f, url=f"{f.base_url}/", label=f"{label} · financial statements"))
                else:
                    skipped += 1
            if f.form == "8-K" and (force or not svc.store.has(f.accession, "exhibit")):
                for name in svc.edgar.exhibit_99_files(f):
                    planned.append(Artifact("exhibit", t, f, url=f"{f.base_url}/{name}", label=f"{label} · {name}"))
        for p in state.get("uploads", []):
            if p.suffix.lower() == ".pdf":
                planned.append(Artifact("pdf", t, None, path=str(p), label=p.name))
            elif force or not svc.store.has(f"upload-{file_digest(p)}", "xlsx_upload"):
                planned.append(Artifact("xlsx", t, None, path=str(p), label=p.name))
            else:
                skipped += 1
        progress("ingest", f"{len(planned)} new source(s) to index, {skipped} already indexed", "start" if planned else "done")
        return {"artifacts": planned}

    def fan_out(state: State):
        arts = state.get("artifacts") or []
        if not arts:
            return "retrieve"
        return [Send("worker", {"artifact": a}) for a in arts]

    def worker(payload, config):
        a: Artifact = payload["artifact"]
        progress("ingest", f"Indexing {a.label or a.kind}…")
        try:
            # Stream the nested graph and forward its progress events: a nested invoke() would swallow them,
            # leaving the UI silent during a long embedding-quota wait.
            res: dict = {}
            for mode, chunk in artifact_graph.stream({"artifact": a}, config=config, stream_mode=["custom", "values"]):
                if mode == "custom":
                    emit(chunk)
                else:
                    res = chunk
        except Exception as exc:  # noqa: BLE001 - one bad source must not sink the brief
            progress("ingest", f"Failed: {a.label or a.kind}", "info")
            return {"results": [{"key": a.key, "records": 0, "error": str(exc)}], "warnings": [f"{a.label or a.kind} could not be indexed: {short_error(exc)}. It will be retried on the next lookup."]}
        n = res.get("indexed", 0)
        progress("ingest", f"Indexed {n} passages/rows from {a.label or a.kind}")
        return {"results": [{"key": a.key, "records": n}], "warnings": res.get("warnings", [])}

    def retrieve(state: State) -> State:
        progress("retrieve", "Retrieving evidence for each facet…", "start")
        try:
            registry, by_facet = svc.retriever.retrieve(state["company"].ticker, state["accessions"])
        except QuotaExhausted:
            raise  # already a clear, actionable message
        except Exception as exc:  # noqa: BLE001 - e.g. embedding provider down
            raise NoEvidence(f"No evidence could be retrieved ({short_error(exc)}).") from exc
        if not registry:
            detail = "; ".join(w for w in state.get("warnings", [])[:3]) or "nothing could be indexed"
            raise NoEvidence(f"No evidence could be retrieved ({detail}).")
        progress("retrieve", f"{len(registry)} evidence items across {len(by_facet)} facets", "done")
        return {"registry": registry, "by_facet": by_facet}

    def summarize(state: State, config) -> State:
        progress("summarize", f"Drafting cited claims with {svc.summarizer.name}…", "start")
        draft = svc.summarizer.draft(state["company"].name, state["registry"], state["by_facet"], config)
        claims = [{**c.model_dump(), "ok": None, "reasons": []} for c in draft.claims]
        used = getattr(draft, "_model_used", "") or svc.summarizer.name
        progress("summarize", f"{len(claims)} claims drafted by {used}", "done")
        return {"claims": claims, "llm_used": used, "claims_drafted": len(claims)}

    def verify(state: State) -> State:
        reg = {a: to_record(e) for a, e in state["registry"].items()}
        claims, failures = [], []
        for c in state["claims"]:
            r = verify_claim(c, reg)
            c2 = {**c, "ok": r.ok, "reasons": r.reasons}
            claims.append(c2)
            if not r.ok:
                failures.append({"claim": c2, "reasons": r.reasons})
        first = state.get("first_pass_failed") if state.get("retries", 0) > 0 else len(failures)
        progress("verify", f"{len(claims) - len(failures)} of {len(claims)} claims passed source verification", "done")
        return {"claims": claims, "failures": failures, "first_pass_failed": first}

    def after_verify(state: State) -> str:
        return "repair" if state.get("failures") and state.get("retries", 0) < 1 else "finalize"

    def repair(state: State, config) -> State:
        n = len(state["failures"])
        progress("verify", f"Re-prompting {n} failed claim(s) once…", "start")
        keep = [c for c in state["claims"] if c["ok"]]
        try:
            fixed = svc.summarizer.repair(state["company"].name, state["registry"], state["by_facet"], state["failures"], config)
            new = [{**c.model_dump(), "ok": None, "reasons": [], "repaired": True} for c in fixed.claims]
            return {"claims": keep + new, "retries": state.get("retries", 0) + 1}
        except Exception as exc:  # noqa: BLE001
            return {"claims": keep, "retries": state.get("retries", 0) + 1, "warnings": [f"Repair step failed ({short_error(exc)}); unverified claims were dropped."]}

    def finalize(state: State) -> State:
        reg = {a: to_record(e) for a, e in state["registry"].items()}
        ok = [c for c in state["claims"] if c["ok"]]
        # Claims that failed the first pass and were not replaced by a verified repair were dropped.
        repaired_ok = sum(1 for c in ok if c.get("repaired"))
        removed = max(0, state.get("first_pass_failed", 0) - repaired_ok)
        refs: dict[str, int] = {}
        sections = []
        for facet in FACETS:
            items = []
            for c in (c for c in ok if c["facet"] == facet.key):
                cites = []
                for ct in c["citations"]:
                    refs.setdefault(ct["source_id"], len(refs) + 1)
                    cites.append({"ref": refs[ct["source_id"]], "quote": ct["quote"]})
                items.append({"text": c["text"], "citations": cites})
            if items:
                sections.append({"facet": facet.key, "title": facet.title, "claims": items})
        sources = []
        for sid, ref in sorted(refs.items(), key=lambda kv: kv[1]):
            r = reg[sid]
            sources.append(
                {
                    "ref": ref, "kind": r.source_type, "label": r.citation_label(), "url": r.url or None, "form": r.form,
                    "item": r.item, "filed": r.filed or None, "accession": r.accession, "file": r.file or None,
                    "sheet": r.sheet or None, "row": r.row or None,
                }
            )
        warnings = list(state.get("warnings", []))
        if not ok:
            warnings.append("No claim survived source verification, so no summary is shown.")
        brief = {
            "ticker": state["company"].ticker,
            "company": state["company"].name,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "filings": [{"form": f.form, "filed": f.filed, "accession": f.accession, "url": f.doc_url} for f in state["filings"]],
            "sections": sections,
            "sources": sources,
            "removed_claims": removed,
            "warnings": warnings,
            "meta": {
                "llm": state.get("llm_used") or svc.summarizer.name, "embedding": f"{svc.embedder.model} ({svc.embedder.dim}d)",
                "first_pass_failed": state.get("first_pass_failed", 0), "retries": state.get("retries", 0),
                "evidence_items": len(reg), "cache_hit": False,
                "figure_sources": sorted({r.origin for r in reg.values() if r.source_type == "sheet"}),
            },
        }
        # Never cache a brief built while some filing failed to index: it would hide the fix for a day.
        incomplete = any(r.get("error") for r in state.get("results", []))
        brief["meta"]["complete"] = not incomplete
        if ok and not incomplete:
            svc.cache.set_json(state["cache_key"], brief, ttl=cfg.brief_cache_ttl)
        progress("finalize", "Brief ready", "done")
        return {"brief": brief}

    # ------------------------------------------------------------------ wiring
    g = StateGraph(State)
    for name, fn in (
        ("resolve", resolve), ("fetch_quote", fetch_quote), ("plan_ingest", plan_ingest), ("worker", worker),
        ("retrieve", retrieve), ("summarize", summarize), ("verify", verify), ("repair", repair), ("finalize", finalize),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "resolve")
    g.add_conditional_edges("resolve", lambda s: ["fetch_quote"] if s.get("cache_hit") else ["fetch_quote", "plan_ingest"], ["fetch_quote", "plan_ingest"])
    g.add_edge("fetch_quote", END)
    g.add_conditional_edges("plan_ingest", fan_out, ["worker", "retrieve"])
    g.add_edge("worker", "retrieve")
    g.add_edge("retrieve", "summarize")
    g.add_edge("summarize", "verify")
    g.add_conditional_edges("verify", after_verify, ["repair", "finalize"])
    g.add_edge("repair", "verify")
    g.add_edge("finalize", END)
    return g.compile()


def run_config(ticker: str, svc: Services) -> dict:
    """LangSmith run metadata: filter and slice traces by ticker, model and provider."""
    return {
        "run_name": "stock_brief",
        "tags": ["stock-briefing", f"ticker:{ticker}"],
        "metadata": {"ticker": ticker, "llm": svc.summarizer.name, "embedding": svc.embedder.model, "prompt_version": PROMPT_VERSION},
    }
