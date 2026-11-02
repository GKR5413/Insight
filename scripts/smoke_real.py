"""End-to-end run against LIVE SEC EDGAR with offline embeddings + extractive summarizer.

Verifies fetching, parsing, indexing, retrieval, verification and caching without any API key,
and compares the two sources of statement figures:

    SEC_USER_AGENT='Your Name you@example.com' python scripts/smoke_real.py AAPL
    SEC_USER_AGENT='...' python scripts/smoke_real.py JPM --fsds data/fsds.sqlite

It does NOT exercise Gemini (use scripts/check_gemini.py for that).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefing.config import Settings  # noqa: E402
from briefing.graphs.brief import build_brief_graph  # noqa: E402
from briefing.run import stream_brief  # noqa: E402
from briefing.services import build_services  # noqa: E402


def one_run(ticker: str, fsds: str | None) -> dict | None:
    tmp = Path(tempfile.mkdtemp(prefix="briefing-smoke-"))
    settings = Settings(
        data_dir=tmp, milvus_uri=str(tmp / "milvus.db"), llm_provider="offline", embeddings_provider="fake", embedding_dim=256, redis_url="",
        quote_providers="cboe", use_fsds=bool(fsds), fsds_path=fsds or "",
    )
    svc = build_services(settings)
    graph = build_brief_graph(svc)
    t0 = time.time()
    events = list(stream_brief(svc, graph, ticker))
    dt = time.time() - t0
    err = next((e for e in events if e["type"] == "error"), None)
    if err:
        print(f"  ERROR: {err['message']}")
        return None
    brief = next(e for e in events if e["type"] == "result")["brief"]
    origins = Counter(
        r["origin"] for r in svc.store.client.query(svc.store.collection, filter='source_type == "sheet"', output_fields=["origin"], limit=16000)
    )
    print(f"  {dt:5.1f}s | SEC requests: {svc.edgar.requests_made:>3} | figure rows indexed by origin: {dict(origins)}")
    print(f"  cited sheet sources: {[s['label'] for s in brief['sources'] if s['kind'] == 'sheet'][:3]}")
    return brief


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", nargs="?", default="AAPL")
    ap.add_argument("--fsds", help="path to fsds.sqlite; when given, compares R-page scraping against FSDS")
    args = ap.parse_args()
    print(f"{args.ticker}: R pages only")
    base = one_run(args.ticker, None)
    if args.fsds:
        print(f"{args.ticker}: FSDS preferred (fallback to R pages when the data set lacks the filing)")
        one_run(args.ticker, args.fsds)
    return 0 if base else 1


if __name__ == "__main__":
    raise SystemExit(main())
