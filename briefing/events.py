"""Progress events for the SSE stream, and readable error text.

Provider errors arrive as multi-hundred-character JSON blobs; short_error() turns them into one
line a person can act on, and retry_delay() reads the provider's own "retry in N s" hint.
"""
from __future__ import annotations

import re


def emit(payload: dict) -> None:
    """Send an event to the SSE stream. A no-op when the graph is not being streamed."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:  # noqa: BLE001
        pass


def progress(stage: str, message: str, status: str = "info") -> None:
    emit({"type": "progress", "stage": stage, "status": status, "message": message})


_KNOWN = (
    ("RESOURCE_EXHAUSTED", "quota or rate limit exceeded (429)"),
    ("429", "quota or rate limit exceeded (429)"),
    ("UNAVAILABLE", "service overloaded (503)"),
    ("503", "service overloaded (503)"),
    ("DEADLINE_EXCEEDED", "request timed out (504)"),
    ("504", "request timed out (504)"),
    ("NOT_FOUND", "model not found or retired (404)"),
    ("API_KEY_INVALID", "API key rejected"),
    ("PERMISSION_DENIED", "permission denied (403)"),
)


def short_error(exc: BaseException | str, limit: int = 160) -> str:
    """One readable line for an exception (or its text)."""
    text = str(exc)
    for needle, nice in _KNOWN:
        if needle in text:
            return nice
    m = re.search(r"'message': '(.*?)'", text, re.S) or re.search(r'"message": "(.*?)"', text, re.S)
    if m:
        text = m.group(1)
    text = re.sub(r"https?://\S+", "", text.replace("\\n", " "))
    text = re.sub(r"\s+", " ", text).strip()
    return (text[: limit - 1] + "…") if len(text) > limit else text or exc.__class__.__name__


def retry_delay(exc: BaseException | str) -> float | None:
    """Seconds the provider asks us to wait ("Please retry in 52.7s" / retryDelay: '52s'), if stated."""
    text = str(exc)
    m = re.search(r"retry in ([\d.]+)\s*s", text, re.I) or re.search(r"retryDelay['\"]?:\s*['\"]?([\d.]+)s", text)
    return float(m.group(1)) if m else None
