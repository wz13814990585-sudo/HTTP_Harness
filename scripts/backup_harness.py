"""Run only after stopping API/worker writes; see docs/P08_local_runbook.md."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from hnh.backup import backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blob-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    database_url = os.environ.get("HNH_DATABASE_URL")
    if not database_url:
        parser.error("HNH_DATABASE_URL is required")
    manifest = backup(database_url, args.blob_root, args.destination)
    print(json.dumps({"backup": str(args.destination), "blob_count": len(manifest["blobs"])}))


if __name__ == "__main__":
    main()
