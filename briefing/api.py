"""FastAPI app: a health check and the brief endpoints (SSE stream + blocking JSON)."""
from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, get_settings
from .embeddings import ConfigError
from .graphs.brief import build_brief_graph
from .run import friendly_error, stream_brief
from .services import Services, build_services
from .sources.edgar import EdgarError

TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")


class Runtime:
    """Lazily builds services so the app starts (and /api/health explains what is missing)
    even when configuration is incomplete."""

    def __init__(self, factory=build_services):
        self._factory = factory
        self._svc: Services | None = None
        self._graph = None
        self._lock = threading.Lock()
        self.ticker_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def get(self) -> tuple[Services, object]:
        with self._lock:
            if self._svc is None:
                self._svc = self._factory()
                self._graph = build_brief_graph(self._svc)
            return self._svc, self._graph


def create_app(factory=build_services) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Stock Briefing", version="0.1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()], allow_methods=["GET"], allow_headers=["*"]
    )
    rt = Runtime(factory)

    def services_or_503() -> tuple[Services, object]:
        try:
            return rt.get()
        except (ConfigError, EdgarError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def check_ticker(ticker: str) -> str:
        if not TICKER_RE.match(ticker):
            raise HTTPException(status_code=422, detail="Enter a ticker symbol such as AAPL or BRK-B.")
        return ticker.upper()

    @app.get("/api/health")
    def health():
        s = get_settings()
        problems = []
        if "@" not in s.sec_user_agent:
            problems.append("SEC_USER_AGENT is not set (needs a contact email).")
        if s.llm_provider == "gemini" and not s.google_api_key:
            problems.append("GOOGLE_API_KEY is not set.")
        body = {
            "ok": not problems,
            "problems": problems,
            "config": {
                "llm": f"{s.llm_provider}:{s.llm_model}" if s.llm_provider != "offline" else "offline (no LLM)",
                "embeddings": f"{s.embeddings_provider}:{s.embedding_model}:{s.embedding_dim}",
                "milvus": s.resolved_milvus_uri,
                "quote_providers": s.quote_providers,
                "langsmith_tracing": bool(s.langsmith_tracing and s.langsmith_api_key),
            },
        }
        try:
            svc, _ = rt.get()
            body["cache_backend"] = svc.cache.backend
            body["evidence_rows"] = svc.store.count()
            body["fsds"] = {"available": True, **svc.fsds.coverage()} if svc.fsds else {"available": False}
        except Exception as exc:  # noqa: BLE001
            body["ok"] = False
            if str(exc) not in problems:
                problems.append(str(exc))
        return body

    @app.get("/api/brief/stream")
    def brief_stream(ticker: str = Query(...), refresh: bool = False):
        t = check_ticker(ticker)
        svc, graph = services_or_503()

        def gen():
            with rt.ticker_locks[t]:  # one run per ticker at a time: the second hits the warm caches
                for ev in stream_brief(svc, graph, t, refresh):
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/brief")
    def brief(ticker: str = Query(...), refresh: bool = False):
        t = check_ticker(ticker)
        svc, graph = services_or_503()
        with rt.ticker_locks[t]:
            for ev in stream_brief(svc, graph, t, refresh):
                if ev["type"] == "result":
                    return {"quote": ev["quote"], "brief": ev["brief"]}
                if ev["type"] == "error":
                    return JSONResponse({"error": ev["message"]}, status_code=502)
        return JSONResponse({"error": "no result"}, status_code=502)

    dist = Path(ROOT) / "frontend" / "dist"
    if dist.is_dir():  # `make build` output: serve the SPA from the same origin
        app.mount("/", StaticFiles(directory=dist, html=True), name="spa")
    return app


app = create_app()
