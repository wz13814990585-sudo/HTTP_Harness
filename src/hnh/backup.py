"""Explicit offline DB+blob backup/restore; operators must quiesce writers first."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def _postgres_uri(database_url: str) -> str:
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+psycopg":
        raise ValueError("backup requires postgresql+psycopg")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _pg_binary(name: str) -> str:
    binary_dir = subprocess.run(
        ["pg_config", "--bindir"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    binary = Path(binary_dir) / name
    if not binary.is_file():
        raise RuntimeError(f"PostgreSQL {name} is unavailable")
    return str(binary)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(database_url: str, blob_root: Path, destination: Path) -> dict[str, Any]:
    """Write a new verified snapshot directory; never overwrite an old backup."""
    if destination.exists():
        raise FileExistsError("backup destination already exists")
    if not destination.parent.is_dir():
        raise FileNotFoundError("backup parent directory is missing")
    blobs = blob_root / "blobs"
    if not blobs.is_dir() or blobs.is_symlink():
        raise ValueError("configured blob directory is unavailable or a symlink")
    stage = Path(tempfile.mkdtemp(prefix=".hnh-backup-", dir=destination.parent))
    try:
        database_dump = stage / "database.dump"
        subprocess.run(
            [
                _pg_binary("pg_dump"),
                "--format=custom",
                "--file",
                str(database_dump),
                _postgres_uri(database_url),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        archived_blobs = stage / "blobs"
        archived_blobs.mkdir()
        blob_hashes: dict[str, str] = {}
        for source in sorted(blobs.iterdir()):
            if source.is_symlink() or not source.is_file():
                raise ValueError("blob directory contains a non-regular entry")
            digest = _digest(source)
            if source.name != digest:
                raise ValueError("blob filename does not match content digest")
            shutil.copyfile(source, archived_blobs / source.name)
            blob_hashes[source.name] = digest
        manifest: dict[str, Any] = {
            "format": "hnh-offline-backup-v1",
            "database_sha256": _digest(database_dump),
            "blobs": blob_hashes,
            "consistency": "writers_must_be_quiesced_by_operator",
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(stage, destination)
        return manifest
    except BaseException:
        shutil.rmtree(stage)
        raise


def restore(source: Path, target_database_url: str, target_blob_root: Path) -> dict[str, Any]:
    """Restore only into an explicitly empty database and blob root; starts no workers."""
    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("backup manifest symlinks are forbidden")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != "hnh-offline-backup-v1":
        raise ValueError("unsupported backup format")
    database_dump = source / "database.dump"
    if database_dump.is_symlink() or _digest(database_dump) != manifest.get("database_sha256"):
        raise ValueError("database backup checksum mismatch")
    blob_hashes = manifest.get("blobs")
    if not isinstance(blob_hashes, dict):
        raise ValueError("backup blob manifest is invalid")
    if target_blob_root.exists() and any(target_blob_root.iterdir()):
        raise FileExistsError("restore blob root must be empty")
    for name, digest in blob_hashes.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError("backup blob manifest is invalid")
        if len(name) != 64 or any(character not in "0123456789abcdef" for character in name):
            raise ValueError("backup blob name is invalid")
        blob = source / "blobs" / name
        if blob.is_symlink() or _digest(blob) != digest or name != digest:
            raise ValueError("backup blob checksum mismatch")
    engine = create_engine(target_database_url, pool_pre_ping=True)
    try:
        if inspect(engine).get_table_names():
            raise ValueError("restore target database must be empty")
    finally:
        engine.dispose()
    subprocess.run(
        [
            _pg_binary("pg_restore"),
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            _postgres_uri(target_database_url),
            str(database_dump),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    target_blobs = target_blob_root / "blobs"
    target_blobs.mkdir(parents=True, exist_ok=True)
    for name in blob_hashes:
        shutil.copyfile(source / "blobs" / name, target_blobs / name)
    engine = create_engine(target_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            active_runs = connection.execute(
                text(
                    "SELECT count(*) FROM runs WHERE status NOT IN "
                    "('succeeded','failed','cancelled','expired')"
                )
            ).scalar_one()
            unknown_actions = connection.execute(
                text("SELECT count(*) FROM actions WHERE status = 'outcome_unknown'")
            ).scalar_one()
    finally:
        engine.dispose()
    return {
        "active_runs_requiring_recovery_review": int(active_runs),
        "unknown_actions_requiring_reconciliation": int(unknown_actions),
        "workers_started": False,
    }
