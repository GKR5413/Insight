"""Load SEC Financial Statement Data Sets (extracted quarters) into data/fsds.sqlite.

Get the quarters from https://www.sec.gov/dera/data/financial-statement-data-sets and unzip each
one into its own folder, e.g. data/raw/2026q1/ (sub.txt, num.txt, pre.txt, tag.txt). Then:

    python scripts/ingest_fsds.py                      # every data/raw/<YYYYqN> folder
    python scripts/ingest_fsds.py --quarters 2026q3    # just one (re-running a quarter replaces it)
    python scripts/ingest_fsds.py --tickers path/to/company_tickers.json

Ticker -> CIK needs SEC's company_tickers.json. If --tickers is not given, the copy the app caches
at data/cache/edgar/company_tickers.json is used when it exists.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefing.config import get_settings  # noqa: E402
from briefing.sources.fsds import DEFAULT_FORMS, FsdsError, connect, ingest_quarter, load_tickers  # noqa: E402

QUARTER = re.compile(r"^\d{4}q[1-4]$")


def main() -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", type=Path, default=settings.fsds_raw_dir)
    ap.add_argument("--db", type=Path, default=settings.fsds_db)
    ap.add_argument("--quarters", nargs="*", help="quarter folder names, e.g. 2026q1 (default: all found)")
    ap.add_argument("--forms", nargs="*", default=list(DEFAULT_FORMS), help="SEC form types to keep")
    ap.add_argument("--tickers", type=Path, default=settings.edgar_cache_dir / "company_tickers.json")
    args = ap.parse_args()

    found = sorted(p.name for p in args.raw_dir.iterdir() if p.is_dir() and QUARTER.match(p.name) and (p / "num.txt").exists()) if args.raw_dir.is_dir() else []
    quarters = args.quarters or found
    if not quarters:
        print(f"No extracted quarters found under {args.raw_dir}. Unzip e.g. 2026q1.zip into {args.raw_dir / '2026q1'}/")
        return 1
    bad = [q for q in quarters if not QUARTER.match(q)]
    if bad:
        print(f"Not a quarter name (expected like 2026q1): {bad}")
        return 1

    conn = connect(args.db)
    for q in quarters:
        t0 = time.time()
        try:
            s = ingest_quarter(conn, args.raw_dir / q, q, args.forms)
        except (FsdsError, FileNotFoundError) as exc:
            print(f"{q}: FAILED ({exc})")
            return 1
        print(f"{q}: {s.filings:,} filings, {s.facts:,} facts, {s.lines:,} statement lines, {s.tags:,} new tags "
              f"in {time.time() - t0:.0f}s (repaired {s.repaired}, rejected {s.rejected})")
        for w in s.warnings:
            print(f"  warning: {w}")

    if args.tickers.exists():
        print(f"tickers: {load_tickers(conn, args.tickers):,} loaded from {args.tickers}")
    else:
        print(f"tickers: {args.tickers} not found, so ticker -> CIK lookups will return None.\n"
              "  Pass --tickers, or run the app once (it caches the file), or fetch it with a descriptive User-Agent:\n"
              "  curl -A 'Your Name you@example.com' https://www.sec.gov/files/company_tickers.json -o <path>")
    n = conn.execute("SELECT COUNT(*) FROM latest_filings").fetchone()[0]
    print(f"done: {args.db} ({args.db.stat().st_size / 1e6:,.0f} MB), {n:,} companies/forms in latest_filings")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
