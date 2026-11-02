"""Preflight for the Gemini path (costs a fraction of a cent): run this before your first brief.

    python scripts/check_gemini.py

Checks, in order: key present, embedding model + dimension, structured summarization with a
verifiable citation. It prints the exact model names in use so a retired alias is obvious.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefing.config import get_settings  # noqa: E402
from briefing.embeddings import ConfigError, build_embedder  # noqa: E402
from briefing.llm import build_summarizer  # noqa: E402
from briefing.models import Record  # noqa: E402
from briefing.verify import verify_claim  # noqa: E402

SAMPLE = (
    "The Company designs, manufactures and markets smartphones, personal computers, tablets, wearables and accessories, "
    "and sells a variety of related services. Net sales increased 6% during 2025 compared to 2024."
)


def main() -> int:
    s = get_settings()
    if not s.google_api_key:
        print("GOOGLE_API_KEY is not set. Put it in .env (see .env.example).")
        return 1

    print(f"embedding model: {s.embedding_model} ({s.embedding_dim}d)")
    try:
        t = time.time()
        emb = build_embedder(s.embeddings_provider, s.embedding_model, s.embedding_dim, google_api_key=s.google_api_key, openai_api_key=s.openai_api_key)
        qv = emb.embed_query("risk factors")
        dv = emb.embed_documents(["Supply chain disruption could harm results.", "Net sales increased."])
        assert len(qv) == s.embedding_dim == len(dv[0]), f"expected {s.embedding_dim} dims, got {len(qv)}/{len(dv[0])}"
        print(f"  OK  query+2 documents embedded, {len(qv)} dims, {time.time() - t:.1f}s")
    except Exception as exc:  # noqa: BLE001 - this script exists to surface exactly this
        print(f"  FAIL embeddings: {exc.__class__.__name__}: {exc}")
        return 1

    print(f"llm models (in order): {s.llm_model}, {s.llm_fallback_models}")
    try:
        summ = build_summarizer(s.llm_provider, s.llm_model, s.google_api_key, s.llm_fallback_models)
        rec = Record(
            id="x", ticker="TEST", accession="0000000000-00-000000", form="10-K", filed="2025-10-31", source_type="doc",
            section_key="10-K:1", item="Item 1 — Business", text=SAMPLE,
        )
        registry = {"S1": rec.to_row()}
        t = time.time()
        draft = summ.draft("Test Co", registry, {"business": ["S1"], "financials": ["S1"]})
        print(f"  OK  structured output parsed: {len(draft.claims)} claim(s) from {draft._model_used}, {time.time() - t:.1f}s")
        from briefing.facets import to_record

        reg = {"S1": to_record(registry["S1"])}
        results = [verify_claim(c.model_dump(), reg) for c in draft.claims]
        for c, r in zip(draft.claims, results, strict=True):
            print(f"  - [{'verified' if r.ok else 'FAILED: ' + '; '.join(r.reasons)}] ({c.facet}) {c.text}")
        if not any(r.ok for r in results):
            print("  WARN no claim passed verification; inspect the prompt/model before relying on briefs.")
            return 2
    except ConfigError as exc:
        print(f"  FAIL: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL llm: {exc.__class__.__name__}: {exc}")
        return 1
    print("Gemini path looks good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
