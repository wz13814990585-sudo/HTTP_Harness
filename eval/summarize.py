"""Summarize immutable evaluation JSONL without dropping failed or blocked rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hnh.evaluation import summarize


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_jsonl", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.raw_jsonl.read_text().splitlines() if line]
    summary = summarize(rows)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("design_complete") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
