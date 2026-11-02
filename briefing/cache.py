"""Small JSON cache: Redis when reachable, in-process dict otherwise.

Redis is only a hot cache here (quotes, filing indexes, finished briefs). Durable evidence
lives in Milvus, so losing Redis never loses data, it only costs a recompute.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


class Cache:
    def __init__(self, redis_url: str | None = None):
        self._mem: dict[str, tuple[float | None, str]] = {}
        self._lock = threading.Lock()
        self._redis = None
        self.backend = "memory"
        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=2, decode_responses=True)
                client.ping()
                self._redis = client
                self.backend = "redis"
            except Exception as exc:  # noqa: BLE001 - any failure means "no redis"
                log.warning("Redis unavailable (%s); using in-memory cache", exc.__class__.__name__)

    @classmethod
    def with_client(cls, client) -> "Cache":
        """Inject an existing client (tests use fakeredis)."""
        c = cls(None)
        c._redis = client
        c.backend = "redis"
        return c

    def get_json(self, key: str) -> Any | None:
        raw = self._get(key)
        return None if raw is None else json.loads(raw)

    def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._set(key, json.dumps(value), ttl)

    def delete(self, key: str) -> None:
        if self._redis is not None:
            try:
                self._redis.delete(key)
                return
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._mem.pop(key, None)

    # -- internals ------------------------------------------------------------------
    def _get(self, key: str) -> str | None:
        if self._redis is not None:
            try:
                return self._redis.get(key)
            except Exception as exc:  # noqa: BLE001
                log.warning("Redis get failed (%s); falling back to memory", exc.__class__.__name__)
        with self._lock:
            hit = self._mem.get(key)
            if not hit:
                return None
            expires, raw = hit
            if expires is not None and expires < time.time():
                del self._mem[key]
                return None
            return raw

    def _set(self, key: str, raw: str, ttl: int | None) -> None:
        if self._redis is not None:
            try:
                self._redis.set(key, raw, ex=ttl)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Redis set failed (%s); falling back to memory", exc.__class__.__name__)
        with self._lock:
            self._mem[key] = (time.time() + ttl if ttl else None, raw)
