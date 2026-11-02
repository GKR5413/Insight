"""Milvus wrapper (Milvus Lite by default; set MILVUS_URI=http://host:19530 for a server).

Filterable fields are real schema columns; everything else rides along as dynamic fields.
The collection name encodes the embedding model and dimension.
"""
from __future__ import annotations

import json
import re
import threading

from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException

from .embeddings import ConfigError
from .models import Record

OUTPUT_FIELDS = ["id", "ticker", "accession", "form", "filed", "source_type", "section_key", "item", "text", "url", "file", "sheet", "row", "cells", "origin"]
_SAFE = re.compile(r"^[A-Za-z0-9:._\-]+$")


def _safe(value: str) -> str:
    if not _SAFE.match(value):
        raise ValueError(f"unsafe filter value: {value!r}")
    return value


def build_filter(ticker: str, accessions: list[str], *, section_keys: list[str] | None = None, source_type: str | None = None, form: str | None = None) -> str:
    parts = [f'ticker == "{_safe(ticker)}"', f"accession in {json.dumps([_safe(a) for a in accessions])}"]
    if section_keys:
        parts.append(f"section_key in {json.dumps([_safe(k) for k in section_keys])}")
    if source_type:
        parts.append(f'source_type == "{_safe(source_type)}"')
    if form:
        parts.append(f'form == "{_safe(form)}"')
    return " and ".join(parts)


class VectorStore:
    def __init__(self, uri: str, model: str, dim: int):
        try:
            self.client = MilvusClient(uri)
        except Exception as exc:  # noqa: BLE001 - pymilvus raises several unrelated types here
            if "://" in uri:
                hint = "Start a Milvus server there, or clear MILVUS_URI in .env to use the embedded Milvus Lite file (data/milvus.db)."
            else:
                hint = "Check that the file is writable, or delete it to start fresh."
            raise ConfigError(f"Cannot open Milvus at {uri} ({exc.__class__.__name__}). {hint}") from exc
        slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
        self.collection = f"filings_{slug}_{dim}"
        self.dim = dim
        self._lock = threading.Lock()  # Milvus Lite is embedded; serialise access to be safe
        self._ensure()
        self._load()

    def _ensure(self) -> None:
        if self.client.has_collection(self.collection):
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=200)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dim)
        schema.add_field("ticker", DataType.VARCHAR, max_length=16)
        schema.add_field("accession", DataType.VARCHAR, max_length=64)
        schema.add_field("form", DataType.VARCHAR, max_length=16)
        schema.add_field("source_type", DataType.VARCHAR, max_length=8)
        schema.add_field("section_key", DataType.VARCHAR, max_length=48)
        index = self.client.prepare_index_params()
        index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(self.collection, schema=schema, index_params=index)

    def _load(self) -> None:
        self.client.load_collection(self.collection)

    def _call(self, fn):
        """Run a Milvus call; if the collection was released (e.g. the embedded server's connection reset
        and came back unloaded), load it and retry once."""
        try:
            return fn()
        except MilvusException as exc:
            if getattr(exc, "code", None) == 101 or "released" in str(exc) or "not loaded" in str(exc).lower():
                self._load()
                return fn()
            raise

    def has(self, accession: str, origin: str) -> bool:
        with self._lock:
            rows = self._call(
                lambda: self.client.query(
                    self.collection,
                    filter=f'accession == "{_safe(accession)}" and origin == "{_safe(origin)}"',
                    output_fields=["id"],
                    limit=1,
                )
            )
        return bool(rows)

    def upsert(self, records: list[Record], vectors: list[list[float]]) -> int:
        if not records:
            return 0
        rows = [{**r.to_row(), "vector": v} for r, v in zip(records, vectors, strict=True)]
        with self._lock:
            self._call(lambda: self.client.upsert(self.collection, rows))
        return len(rows)

    def search(self, vector: list[float], expr: str, limit: int) -> list[dict]:
        with self._lock:
            res = self._call(lambda: self.client.search(self.collection, data=[vector], filter=expr, limit=limit, output_fields=OUTPUT_FIELDS))
        return [{**hit["entity"], "id": hit["id"], "score": float(hit["distance"])} for hit in res[0]]

    def count(self) -> int:
        with self._lock:
            return int(self._call(lambda: self.client.get_collection_stats(self.collection))["row_count"])
