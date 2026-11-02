"""Runs the brief graph and turns it into a stream of UI events."""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator

from .embeddings import ConfigError
from .graphs.brief import NoEvidence, run_config
from . import quality
from .llm import LLMUnavailable
from .services import Services
from .sources.edgar import EdgarError

log = logging.getLogger(__name__)


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, (ConfigError, EdgarError, NoEvidence, LLMUnavailable)):
        return str(exc)
    log.exception("brief failed")
    return f"Something went wrong ({exc.__class__.__name__}): {exc}"


def stream_brief(svc: Services, graph, ticker: str, refresh: bool = False) -> Iterator[dict]:
    """Yields {'type': 'progress'|'quote'|'result'|'error', ...} events.

    Every outcome is also written to the quality log (and scored on the LangSmith run when tracing is
    on). The run id is chosen here so feedback can be attached to exactly this trace.
    """
    final: dict = {}
    run_id = str(uuid.uuid4())
    qdir = svc.settings.data_dir / "quality"
    t0 = time.time()
    try:
        for mode, chunk in graph.stream(
            {"ticker": ticker, "refresh": refresh}, config={**run_config(ticker, svc), "run_id": run_id}, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield chunk
            else:
                final = chunk
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a readable message
        message = friendly_error(exc)
        quality.record_run(qdir, quality.error_metrics(ticker, message, time.time() - t0, run_id))
        yield {"type": "error", "message": message}
        return
    brief = final.get("brief")
    if brief is None:
        message = "The pipeline finished without producing a brief."
        quality.record_run(qdir, quality.error_metrics(ticker, message, time.time() - t0, run_id))
        yield {"type": "error", "message": message}
        return
    if final.get("cache_hit"):
        brief = {**brief, "meta": {**brief.get("meta", {}), "cache_hit": True}}
        quality.record_run(qdir, quality.cache_hit_metrics(ticker, run_id))
    else:
        quality.record_run(qdir, quality.brief_metrics(ticker, final, brief, time.time() - t0, run_id))
    yield {"type": "result", "quote": final.get("quote"), "brief": brief, "run_warnings": final.get("warnings", [])}
