"""Facet-based evidence retrieval.

A brief is organised into fixed facets (financials, risks, liquidity, ...). For each facet we
run a few filtered vector searches (scoped to this ticker and the selected filings) and hand the
summarizer only those passages and rows. Sources get short aliases (S1, S2, ...) so the model
cites by alias and code, not the model, resolves aliases to real filings and rows.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .embeddings import Embedder
from .models import Record
from .store import VectorStore, build_filter


@dataclass(frozen=True)
class Spec:
    """One filtered search that contributes evidence to a facet."""

    query: str
    k: int
    section_keys: tuple[str, ...] = ()
    source_type: str | None = None
    form: str | None = None


@dataclass(frozen=True)
class Facet:
    key: str
    title: str
    guidance: str
    specs: tuple[Spec, ...] = field(default_factory=tuple)


FACETS: tuple[Facet, ...] = (
    Facet(
        "business", "Business overview",
        "What the company does: products/services, segments, customers, strategy.",
        (Spec("company business overview products services segments customers strategy competition", 4, ("10-K:1",)),),
    ),
    Facet(
        "financials", "Financial performance",
        "Revenue, profit, margins and per-share results with the drivers of change. Prefer figures from sheet rows; use document passages for the explanation.",
        (
            Spec("results of operations net sales revenue increased decreased compared to prior year drivers gross margin operating expenses", 5, ("10-K:7", "10-Q:I.2")),
            Spec("net sales total revenue cost of sales gross margin operating income net income earnings per share diluted", 8, source_type="sheet"),
        ),
    ),
    Facet(
        "liquidity", "Liquidity & capital",
        "Cash, investments, debt, capital returns (buybacks/dividends), cash flow.",
        (
            Spec("liquidity and capital resources cash cash equivalents marketable securities debt share repurchases dividends", 4, ("10-K:7", "10-Q:I.2")),
            Spec("cash and cash equivalents marketable securities term debt commercial paper total liabilities shareholders equity repurchases of common stock dividends operating activities cash", 6, source_type="sheet"),
        ),
    ),
    Facet(
        "risks", "Key risks",
        "The principal risks and any stated changes to risk factors.",
        (Spec("principal risk factors material risks supply chain regulation competition litigation macroeconomic conditions changes to risk factors", 6, ("10-K:1A", "10-Q:II.1A")),),
    ),
    Facet(
        "outlook", "Trends & outlook",
        "Management's stated trends, uncertainties and expectations. Do not infer guidance that is not stated.",
        (Spec("outlook expectations trends uncertainties anticipate expect forward-looking next quarter fiscal year", 4, ("10-K:7", "10-Q:I.2", "8-K:EX99.1", "8-K:EX99")),),
    ),
    Facet(
        "events", "Recent events (8-K)",
        "Material events disclosed in recent 8-K filings: results releases, leadership changes, agreements.",
        (Spec("material event announcement results of operations press release agreement officer director change", 6, form="8-K"),),
    ),
    Facet(
        "legal", "Legal proceedings",
        "Material legal proceedings described in the filings.",
        (Spec("legal proceedings litigation lawsuit court regulatory investigation fine", 3, ("10-K:3", "10-Q:II.1")),),
    ),
)
FACET_BY_KEY = {f.key: f for f in FACETS}


class Retriever:
    """The facet queries are fixed text, so their vectors are cached on disk per embedding model:
    after the first brief, retrieval costs zero embedding calls (it matters on a free quota)."""

    def __init__(self, store: VectorStore, embedder: Embedder, cache_path: Path | None = None):
        self.store = store
        self.embedder = embedder
        self._path = Path(cache_path) if cache_path else None
        self._qcache: dict[str, list[float]] = {}
        if self._path and self._path.exists():
            try:
                self._qcache = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                self._qcache = {}

    def _qvec(self, q: str) -> list[float]:
        if q not in self._qcache:
            self._qcache[q] = self.embedder.embed_query(q)
            self._save()
        return self._qcache[q]

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._qcache))
            os.replace(tmp, self._path)
        except OSError:
            pass  # a cache that cannot be written is only a missed optimisation

    def retrieve(self, ticker: str, accessions: list[str], facets: tuple[Facet, ...] = FACETS) -> tuple[dict[str, dict], dict[str, list[str]]]:
        """Returns (registry alias -> record dict, facet -> ordered aliases)."""
        registry: dict[str, dict] = {}
        by_id: dict[str, str] = {}
        by_facet: dict[str, list[str]] = {}
        for facet in facets:
            aliases: list[str] = []
            for spec in facet.specs:
                expr = build_filter(ticker, accessions, section_keys=list(spec.section_keys) or None, source_type=spec.source_type, form=spec.form)
                for hit in self.store.search(self._qvec(spec.query), expr, spec.k):
                    alias = by_id.get(hit["id"])
                    if alias is None:
                        alias = f"S{len(registry) + 1}"
                        by_id[hit["id"]] = alias
                        registry[alias] = hit
                    if alias not in aliases:
                        aliases.append(alias)
            if aliases:
                by_facet[facet.key] = aliases
        return registry, by_facet


def to_record(entry: dict) -> Record:
    names = Record.__dataclass_fields__.keys()
    return Record(**{k: v for k, v in entry.items() if k in names})
