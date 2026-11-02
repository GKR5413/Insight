"""Wiring: builds every collaborator once. Graphs receive a Services bundle, which keeps them
testable (tests pass fakes) and keeps provider choice a config concern."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .cache import Cache
from .config import Settings, get_settings
from .embeddings import Embedder, build_embedder
from .facets import Retriever
from .llm import Summarizer, build_summarizer
from .sources.edgar import EdgarClient
from .sources.quotes import QuoteService, build_quote_service
from .sources.fsds import FsdsStore
from .store import VectorStore

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    cache: Cache
    edgar: EdgarClient
    quotes: QuoteService
    embedder: Embedder
    store: VectorStore
    retriever: Retriever
    summarizer: Summarizer
    fsds: FsdsStore | None = None  # bulk statement figures; absent -> R pages are scraped instead


def build_services(settings: Settings | None = None) -> Services:
    s = settings or get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)

    # LangChain/LangSmith read their config from the environment.
    os.environ.setdefault("LANGSMITH_PROJECT", s.langsmith_project)
    if s.langsmith_tracing and s.langsmith_api_key:
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_API_KEY", s.langsmith_api_key)

    cache = Cache(s.redis_url)
    edgar = EdgarClient(
        s.sec_user_agent, cache, s.edgar_cache_dir, lookback_days_8k=s.filings_lookback_days_8k, max_8k=s.max_8k
    )
    quotes = build_quote_service(s.quote_providers, s.finnhub_api_key, cache, s.quote_cache_ttl)
    embedder = build_embedder(
        s.embeddings_provider, s.embedding_model, s.embedding_dim, google_api_key=s.google_api_key, openai_api_key=s.openai_api_key,
        rpm=s.embedding_rpm if s.embeddings_provider == "gemini" else 0,
    )
    store = VectorStore(s.resolved_milvus_uri, embedder.model, embedder.dim)
    fsds = None
    if s.use_fsds and s.fsds_db.exists():
        try:
            fsds = FsdsStore(s.fsds_db)
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop the app; fall back to R pages
            log.warning("FSDS database unusable (%s); falling back to SEC R pages", exc)
    return Services(
        settings=s,
        cache=cache,
        edgar=edgar,
        quotes=quotes,
        embedder=embedder,
        store=store,
        retriever=Retriever(store, embedder, s.data_dir / "cache" / f"queries_{store.collection}.json"),
        summarizer=build_summarizer(s.llm_provider, s.llm_model, s.google_api_key, s.llm_fallback_models),
        fsds=fsds,
    )
