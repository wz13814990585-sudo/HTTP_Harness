"""Restore into a new empty database and blob root; does not start a worker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from hnh.backup import restore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--blob-root", type=Path, required=True)
    args = parser.parse_args()
    database_url = os.environ.get("HNH_RESTORE_DATABASE_URL")
    if not database_url:
        parser.error("HNH_RESTORE_DATABASE_URL is required")
    print(json.dumps(restore(args.source, database_url, args.blob_root), sort_keys=True))


if __name__ == "__main__":
    main()
