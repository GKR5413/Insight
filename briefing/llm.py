"""Summarizers: Gemini (structured output) and an offline extractive stand-in.

The model never writes free text outside a claim, and every claim must cite sources by alias
with a verbatim quote. Code (verify.py) then checks the quotes against the stored sources.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Literal, Protocol

from pydantic import BaseModel, Field, PrivateAttr

from .embeddings import ConfigError
from .events import short_error
from .facets import FACET_BY_KEY, FACETS
from .models import Record
from .facets import to_record

log = logging.getLogger(__name__)
PROMPT_VERSION = "v1"
FacetKey = Literal["business", "financials", "liquidity", "risks", "outlook", "events", "legal"]


class DraftCitation(BaseModel):
    source_id: str = Field(description="Alias of a provided source, e.g. 'S3'. Never invent one.")
    quote: str = Field(description="Verbatim text copied from that source that supports the claim. For a sheet row, copy the figure(s) as written, e.g. '$ 94,036'.")


class DraftClaim(BaseModel):
    facet: FacetKey
    text: str = Field(description="One or two plain sentences that are fully supported by the cited quotes.")
    citations: list[DraftCitation] = Field(min_length=1)


class DraftBrief(BaseModel):
    claims: list[DraftClaim]
    _model_used: str = PrivateAttr(default="")  # not part of the schema sent to the model


SYSTEM = """You write evidence-based briefings on a public company for wealth-management advisors.

Rules:
- Use ONLY the numbered sources provided. Never use outside knowledge, never guess, never extrapolate.
- Every claim must cite one or more sources by alias (S1, S2, ...) and include a verbatim quote from each cited source. Copy quotes exactly (>= 20 characters for passages; for spreadsheet rows copy the figure as written).
- A claim states what the source says. Do not add interpretation, predictions, or recommendations. Never give investment advice.
- Report figures with their period and unit (the row/column headings and 'units' tell you). Do not convert units or round.
- Prefer a few precise claims over many vague ones. If a facet has insufficient evidence, write no claims for it.
- Each claim is one or two sentences. Do not write anything outside the claims."""


def format_sources(registry: dict[str, dict], by_facet: dict[str, list[str]], *, only: set[str] | None = None) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for facet in FACETS:
        aliases = [a for a in by_facet.get(facet.key, []) if only is None or a in only]
        if not aliases:
            continue
        lines.append(f"\n## Facet: {facet.key} — {facet.title}\nGuidance: {facet.guidance}")
        for alias in aliases:
            if alias in seen:
                lines.append(f"(see {alias} above)")
                continue
            seen.add(alias)
            rec = to_record(registry[alias])
            kind = "sheet row" if rec.source_type == "sheet" else "document passage"
            lines.append(f"\n[{alias}] {kind} — {rec.citation_label()}\n{rec.text}")
    return "\n".join(lines)


class Summarizer(Protocol):
    name: str

    def draft(self, company: str, registry: dict[str, dict], by_facet: dict[str, list[str]], config: dict | None = None) -> DraftBrief: ...

    def repair(self, company: str, registry: dict[str, dict], by_facet: dict[str, list[str]], failures: list[dict], config: dict | None = None) -> DraftBrief: ...


class LLMUnavailable(RuntimeError):
    """Every configured model failed for reasons that are not our bug (overload, quota, timeouts)."""


TRANSIENT = ("503", "UNAVAILABLE", "500", "INTERNAL", "504", "DEADLINE", "timed out", "timeout", "overloaded")
SKIP_MODEL = ("404", "NOT_FOUND", "429", "RESOURCE_EXHAUSTED", "quota")


class GeminiSummarizer:
    """Gemini with structured output, a bounded retry on transient errors and an ordered fallback chain.

    503 (overloaded) is retried briefly, then the next model is tried. 429 (quota) and 404 (retired
    alias) skip straight to the next model. Anything else (a 400 for a bad schema, say) is a bug
    that every model would share, so it is raised rather than hidden behind a fallback.
    """

    def __init__(self, models: list[str], api_key: str, *, chains=None, sleep=time.sleep, attempts: int = 2, passes: int = 2, pass_wait: float = 8.0, request_timeout: float = 90.0):
        if not models:
            raise ConfigError("LLM_MODEL is empty.")
        if chains is None:
            if not api_key:
                raise ConfigError("GOOGLE_API_KEY is not set. Add your Gemini API key to .env (see .env.example).")
            from langchain_google_genai import ChatGoogleGenerativeAI

            # Gemini 3.x models fix their sampling parameters (and warn if you pass temperature).
            # An explicit timeout matters: an overloaded model can otherwise hold a request for a minute or more.
            chains = [
                (
                    m,
                    ChatGoogleGenerativeAI(
                        model=m, google_api_key=api_key, max_retries=1, timeout=request_timeout, **({} if m.startswith("gemini-3") else {"temperature": 0})
                    ).with_structured_output(DraftBrief),
                )
                for m in models
            ]
        self._chains = chains
        self._sleep = sleep
        self._attempts = attempts
        self._passes = passes
        self._pass_wait = pass_wait
        self.name = models[0]  # configured primary; the model that answered is recorded on each draft

    def _call(self, system: str, user: str, config: dict | None) -> DraftBrief:
        errors: list[str] = []
        for pass_no in range(self._passes):
            if pass_no:
                self._sleep(self._pass_wait)  # everything was busy: give the service a moment, then go round once more
            errors, saw_transient = [], False
            for model, structured in self._chains:
                for attempt in range(self._attempts):
                    try:
                        out = structured.invoke([("system", system), ("human", user)], config=config)
                        if out is None:
                            raise RuntimeError("returned no structured output")
                        draft = out if isinstance(out, DraftBrief) else DraftBrief.model_validate(out)
                        draft._model_used = model
                        return draft
                    except Exception as exc:  # noqa: BLE001 - classified below
                        msg = f"{exc.__class__.__name__}: {exc}"
                        if any(t in msg for t in SKIP_MODEL):
                            errors.append(f"{model}: {short_error(exc)}")
                            break
                        if any(t in msg for t in TRANSIENT) or "no structured output" in msg:
                            saw_transient = True
                            errors.append(f"{model}: {short_error(exc)}")
                            if attempt + 1 < self._attempts:
                                self._sleep(1.5 * (attempt + 1))
                            continue
                        raise
                log.warning("Gemini model %s failed; trying the next one", model)
            if not saw_transient:  # quota/retired models will not recover in a few seconds
                break
        raise LLMUnavailable(
            "Gemini could not answer right now (" + "; ".join(dict.fromkeys(errors)) + "). The filings are already indexed, so trying again shortly will be fast."
        )

    def draft(self, company, registry, by_facet, config=None):
        user = f"Company: {company}\n\nWrite the briefing claims from these sources.\n{format_sources(registry, by_facet)}"
        return self._call(SYSTEM, user, config)

    def repair(self, company, registry, by_facet, failures, config=None):
        facets = {f["claim"]["facet"] for f in failures}
        scoped = {k: v for k, v in by_facet.items() if k in facets}
        problems = "\n".join(
            f"- facet={f['claim']['facet']} claim={json.dumps(f['claim']['text'])} problems: {'; '.join(f['reasons'])}" for f in failures
        )
        user = (
            f"Company: {company}\n\nSome claims from your previous draft failed source verification. "
            "Rewrite ONLY these claims so each one is supported by a verbatim quote from a provided source, "
            "or omit a claim if no source supports it. Return only the corrected claims.\n\n"
            f"Failed claims:\n{problems}\n\nSources:\n{format_sources(registry, scoped)}"
        )
        return self._call(SYSTEM, user, config)


class OfflineSummarizer:
    """No LLM: quotes the top retrieved evidence verbatim. For demos and tests, clearly labelled."""

    name = "offline-extractive (no LLM)"

    def draft(self, company, registry, by_facet, config=None):
        claims: list[DraftClaim] = []
        for facet in FACETS:
            docs = [a for a in by_facet.get(facet.key, []) if registry[a]["source_type"] == "doc"][:2]
            rows = [a for a in by_facet.get(facet.key, []) if registry[a]["source_type"] == "sheet"][:3]
            for alias in docs:
                sentence = self._first_sentence(registry[alias]["text"])
                if sentence:
                    claims.append(DraftClaim(facet=facet.key, text=sentence, citations=[DraftCitation(source_id=alias, quote=sentence)]))
            for alias in rows:
                rec = to_record(registry[alias])
                cells = rec.cells_dict()
                if not cells:
                    continue
                col, val = next(iter(cells.items()))
                label = rec.text.split(" | ")[2]
                claims.append(
                    DraftClaim(facet=facet.key, text=f"{label}: {val} ({col}).", citations=[DraftCitation(source_id=alias, quote=val)])
                )
        return DraftBrief(claims=claims)

    def repair(self, company, registry, by_facet, failures, config=None):
        return DraftBrief(claims=[])

    @staticmethod
    def _first_sentence(text: str) -> str:
        if text.startswith("[Table]"):
            return ""
        for s in re.split(r"(?<=[.!?])\s+", text.strip()):
            if 40 <= len(s) <= 300:
                return s
        return ""


def build_summarizer(provider: str, model: str, api_key: str, fallbacks: str = "") -> Summarizer:
    if provider == "offline":
        return OfflineSummarizer()
    if provider == "gemini":
        chain = [model, *[m.strip() for m in fallbacks.split(",") if m.strip() and m.strip() != model]]
        return GeminiSummarizer(chain, api_key)
    raise ConfigError(f"Unknown LLM_PROVIDER '{provider}' (use gemini | offline).")
