"""Quality tracking report: briefs, models, failures, golden eval, and (optionally) LangSmith.

    python scripts/quality_report.py                 # from the local quality log + data/eval reports
    python scripts/quality_report.py --langsmith     # also queries your LangSmith project (read-only)
    python scripts/quality_report.py --out report.md

The log (data/quality/briefs.jsonl) is written by every brief, with or without LangSmith.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from briefing import quality  # noqa: E402
from briefing.config import get_settings  # noqa: E402


def main() -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--langsmith", action="store_true", help="include LangSmith project statistics (read-only)")
    ap.add_argument("--out", type=Path, default=s.data_dir / "quality" / "report.md")
    args = ap.parse_args()

    records = quality.load(s.data_dir / "quality")
    golden = [json.load(open(f)) for f in sorted(glob.glob(str(s.data_dir / "eval" / "*.json")), key=os.path.getmtime)]
    ls = None
    if args.langsmith:
        if not s.langsmith_api_key:
            print("LANGSMITH_API_KEY is not set; skipping LangSmith.", file=sys.stderr)
        else:
            try:
                ls = quality.langsmith_summary(s.langsmith_project, s.langsmith_api_key)
            except Exception as exc:  # noqa: BLE001
                print(f"LangSmith query failed ({exc.__class__.__name__}); report continues without it.", file=sys.stderr)
    text = quality.render_report(records, golden, ls)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    print(f"(saved to {args.out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
