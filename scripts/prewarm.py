"""Index and brief tickers ahead of time so a demo is instant (briefs stay cached for 24 hours).

    python scripts/prewarm.py AAPL MSFT JPM          # uses your .env exactly as the API does
    python scripts/prewarm.py --refresh AAPL         # ignore the brief cache and re-index

On Gemini's free tier a cold ticker takes a few minutes (embedding quota is 100 texts/minute);
this is the place to pay that cost once.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefing.graphs.brief import build_brief_graph  # noqa: E402
from briefing.run import stream_brief  # noqa: E402
from briefing.services import build_services  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    svc = build_services()
    graph = build_brief_graph(svc)
    failed = 0
    for t in (x.upper() for x in args.tickers):
        t0 = time.time()
        print(f"== {t}")
        for ev in stream_brief(svc, graph, t, args.refresh):
            if ev["type"] == "progress" and ev["status"] != "start":
                print(f"   {ev['message']}")
            elif ev["type"] == "error":
                print(f"   FAILED: {ev['message']}")
                failed += 1
            elif ev["type"] == "result":
                b = ev["brief"]
                n = sum(len(s["claims"]) for s in b["sections"])
                print(f"   done in {time.time() - t0:.0f}s: {n} claims, {len(b['sources'])} sources, {b['removed_claims']} removed, complete={b['meta'].get('complete')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
