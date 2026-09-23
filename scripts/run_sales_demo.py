"""Run the disposable sales-report reliability demo on local Docker."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from hnh.adapters.execution.docker import DockerExecutionBroker
from hnh.adapters.postgres.database import build_engine
from hnh.ports.execution import SandboxProfile
from hnh.sales_demo import (
    ApprovalView,
    SalesDemoDeclined,
    SalesDemoError,
    run_sales_demo,
)

POSTGRES_IMAGE = (
    "postgres:14.18@sha256:90da0743e4ed7939b08e2945e0451401b4d7abd792530c7d3cc2da82ed534f08"
)
SANDBOX_IMAGE = "python@sha256:1dd3dca85e22886e44fcad1bb7ccab6691dfa83db52214cf9e20696e095f3e36"
PROFILE_ID = "sales-python-safe"


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, stdout=subprocess.DEVNULL, check=True)


def _wait_for_postgres(container: str, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "hnh", "-d", "hnh"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise SalesDemoError("temporary PostgreSQL did not become ready")


def _ensure_image(image: str) -> None:
    inspected = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspected.returncode == 0:
        return
    print("[setup] Pulling the digest-pinned Python sandbox image", flush=True)
    _run(["docker", "pull", image])


def _approval_prompt(view: ApprovalView) -> bool:
    print("\n[approval] The following exact publication is waiting for you:")
    print(f"  Artifact: {view.artifact_id}")
    print(f"  SHA-256:  {view.artifact_sha256}")
    print("  Target:   registered local publication simulator")
    print("  Effect:   unsafe; an ambiguous response will not be retried")
    try:
        answer = input("Type 'approve' to publish, anything else to deny: ")
    except EOFError:
        return False
    return answer.strip().lower() == "approve"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the sales CSV -> Python -> approval -> recovery demonstration"
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="approve non-interactively for automated verification or recording",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    suffix = secrets.token_hex(4)
    container = f"hnh-sales-postgres-{suffix}"
    password = secrets.token_urlsafe(24)
    port = _available_port()
    database_url = f"postgresql+psycopg://hnh:{password}@127.0.0.1:{port}/hnh"
    broker: DockerExecutionBroker | None = None
    engine = None
    started = False
    try:
        _ensure_image(SANDBOX_IMAGE)
        print("[setup] Starting a disposable PostgreSQL container", flush=True)
        _run(
            [
                "docker",
                "run",
                "--detach",
                f"--name={container}",
                "--label=hnh.managed=true",
                "--env=POSTGRES_DB=hnh",
                "--env=POSTGRES_USER=hnh",
                f"--env=POSTGRES_PASSWORD={password}",
                f"--publish=127.0.0.1:{port}:5432",
                POSTGRES_IMAGE,
            ]
        )
        started = True
        _wait_for_postgres(container)
        migration_environment = dict(os.environ)
        migration_environment["HNH_DATABASE_URL"] = database_url
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=root,
            env=migration_environment,
            check=True,
        )
        engine = build_engine(database_url)
        broker = DockerExecutionBroker(
            (
                SandboxProfile(
                    PROFILE_ID,
                    SANDBOX_IMAGE,
                    cpu_limit=0.5,
                    memory_bytes=64 * 1024 * 1024,
                    pids_limit=32,
                    timeout_seconds=5.0,
                    output_bytes=16 * 1024,
                    artifact_bytes=1024 * 1024,
                    workspace_bytes=8 * 1024 * 1024,
                ),
            )
        )
        with tempfile.TemporaryDirectory(prefix="hnh-sales-blobs-") as blob_dir:
            summary = run_sales_demo(
                engine,
                Path(blob_dir),
                broker,
                profile_id=PROFILE_ID,
                approve=(lambda _view: True) if args.auto_approve else _approval_prompt,
                progress=lambda message: print(message, flush=True),
            )
        print("\n[result] Verified business workflow")
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except SalesDemoDeclined as exc:
        print(f"\n[result] {exc}", file=sys.stderr)
        return 3
    except (SalesDemoError, subprocess.CalledProcessError) as exc:
        print(f"\n[result] sales demo failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if broker is not None:
            broker.close()
        if engine is not None:
            engine.dispose()
        if started:
            print("[cleanup] Removing only the disposable sales-demo PostgreSQL container")
            subprocess.run(
                ["docker", "rm", "--force", container],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
