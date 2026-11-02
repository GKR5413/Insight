"""Embeddings behind one interface so the provider is a config switch.

The model name and dimension travel with the vectors (they name the Milvus collection), because
vectors from different models are not comparable and switching means re-embedding.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from collections import deque
from typing import Protocol

from .events import progress, retry_delay, short_error

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


class QuotaExhausted(ConfigError):
    """A quota that will not clear in minutes (e.g. Gemini's free tier of 1,000 embedded texts per day)."""


def _human(seconds: float) -> str:
    h, m = int(seconds // 3600), int(seconds % 3600 // 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


class EmbeddingsBackend(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbeddings:
    """Deterministic bag-of-words embeddings. Offline demos and tests only: no semantic knowledge."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class RateLimiter:
    """Sliding one-minute window counted in texts (Gemini's free tier counts every text as a request)."""

    def __init__(self, per_minute: int, *, now=time.monotonic):
        self.per_minute = per_minute
        self._now = now
        self._stamps: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def acquire(self, count: int, on_wait=None) -> None:
        if self.per_minute <= 0:
            return
        count = min(count, self.per_minute)  # a single batch can never exceed the window
        while True:
            with self._lock:
                t = self._now()
                while self._stamps and t - self._stamps[0][0] >= 60:
                    self._stamps.popleft()
                if sum(c for _, c in self._stamps) + count <= self.per_minute:
                    self._stamps.append((t, count))
                    return
                wait = 60 - (t - self._stamps[0][0]) + 0.05
            if on_wait:
                on_wait(wait)
            time.sleep(wait)


class Embedder:
    def __init__(self, backend: EmbeddingsBackend, model: str, dim: int, *, batch_size: int = 64, max_concurrent: int = 2, rpm: int = 0):
        self.backend = backend
        self.model = model
        self.dim = dim
        self.rpm = rpm
        self.batch_size = min(batch_size, max(1, rpm // 3)) if rpm else batch_size  # smooth pacing for small quotas
        # Under a rate limit, parallel calls add nothing and collide after a 429, so serialise when paced.
        self._sem = threading.Semaphore(1 if rpm else max_concurrent)
        self._limiter = RateLimiter(rpm)
        self._exhausted_until = 0.0  # monotonic time before which every call fails fast (daily quota)

    @staticmethod
    def _announce_wait(wait: float) -> None:
        if wait >= 5:
            progress("ingest", f"Pausing {wait:.0f}s to stay within the embedding quota…")

    def _retry(self, fn, n, *args):
        delay = 1.5
        for attempt in range(5):
            remaining = self._exhausted_until - time.monotonic()
            if remaining > 0:  # another call already learned the daily quota is gone: do not burn time or pacing tokens
                raise QuotaExhausted(f"Embedding quota used up (Gemini free tier: 1,000 texts/day); resets in ~{_human(remaining)}.")
            self._limiter.acquire(n, self._announce_wait)
            try:
                with self._sem:
                    return fn(*args)
            except Exception as exc:  # noqa: BLE001 - provider errors vary; retry then surface
                if attempt == 4:
                    raise
                hinted = retry_delay(exc)
                if hinted is not None and hinted > 180:  # a daily cap: waiting a few minutes cannot help
                    self._exhausted_until = time.monotonic() + hinted
                    raise QuotaExhausted(f"Embedding quota used up (Gemini free tier: 1,000 texts/day); resets in ~{_human(hinted)}.") from exc
                wait = min(hinted + 1.0, 75.0) if hinted is not None else delay
                log.warning("embedding call failed (%s); retry in %.1fs", short_error(exc), wait)
                if hinted is not None and wait >= 5:
                    progress("ingest", f"Embedding quota reached; waiting {wait:.0f}s as the provider asks…")
                time.sleep(wait)
                delay *= 2

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            out.extend(self._retry(self.backend.embed_documents, len(batch), batch))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._retry(self.backend.embed_query, 1, text)


class _GeminiBackend:
    def __init__(self, model: str, dim: int, api_key: str):
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        self._dim = dim
        name = model if model.startswith("models/") else f"models/{model}"
        self._lc = GoogleGenerativeAIEmbeddings(model=name, google_api_key=api_key)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._lc.embed_documents(texts, task_type="RETRIEVAL_DOCUMENT", output_dimensionality=self._dim)

    def embed_query(self, text: str) -> list[float]:
        return self._lc.embed_query(text, task_type="RETRIEVAL_QUERY", output_dimensionality=self._dim)


class _OpenAIBackend:
    def __init__(self, model: str, dim: int, api_key: str):
        try:
            from langchain_openai import OpenAIEmbeddings
        except ImportError as exc:
            raise ConfigError("EMBEDDINGS_PROVIDER=openai needs `pip install langchain-openai`.") from exc
        self._lc = OpenAIEmbeddings(model=model, dimensions=dim, api_key=api_key)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._lc.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._lc.embed_query(text)


def build_embedder(provider: str, model: str, dim: int, *, google_api_key: str = "", openai_api_key: str = "", rpm: int = 0) -> Embedder:
    if provider == "fake":
        return Embedder(HashingEmbeddings(dim), "hashing", dim)
    if provider == "gemini":
        if not google_api_key:
            raise ConfigError("GOOGLE_API_KEY is not set. Add your Gemini API key to .env (see .env.example).")
        return Embedder(_GeminiBackend(model, dim, google_api_key), model, dim, rpm=rpm)
    if provider == "openai":
        if not openai_api_key:
            raise ConfigError("OPENAI_API_KEY is not set (EMBEDDINGS_PROVIDER=openai).")
        return Embedder(_OpenAIBackend(model, dim, openai_api_key), model, dim)
    raise ConfigError(f"Unknown EMBEDDINGS_PROVIDER '{provider}' (use gemini | openai | fake).")
