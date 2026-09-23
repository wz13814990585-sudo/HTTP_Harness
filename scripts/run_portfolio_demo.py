"""Start a disposable local Compose stack, run the live demo, then clean it up."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import httpx

from hnh.demo import DemoError, run_portfolio_demo


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    if not os.environ.get("HNH_DEEPSEEK_API_KEY"):
        print(
            "portfolio demo configuration missing: HNH_DEEPSEEK_API_KEY "
            "(load your ignored .env first)",
            file=sys.stderr,
        )
        return 2
    root = Path(__file__).resolve().parents[1]
    compose_file = root / "deploy" / "dev" / "compose.yaml"
    suffix = secrets.token_hex(4)
    project = f"hnh-portfolio-{suffix}"
    port = _available_port()
    token = secrets.token_urlsafe(32)
    environment = dict(os.environ)
    environment.update(
        {
            "HNH_DB_PASSWORD": secrets.token_hex(24),
            "HNH_DEV_TOKEN": token,
            "HNH_DEV_TENANT": "portfolio-local",
            "HNH_DEV_SUBJECT": "portfolio-demo",
            "HNH_HTTP_PORT": str(port),
            "HNH_IMAGE_TAG": "portfolio-demo",
            # Keep the recorded demo focused on the Harness and DeepSeek.  A
            # classifier experiment is a separate, independently measured axis.
            "HNH_TYPESAFE_ENABLED": "0",
        }
    )
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(compose_file),
    ]
    started = False
    try:
        print("[setup] Building and starting a disposable local Harness stack")
        subprocess.run(
            [*compose, "up", "--build", "-d", "--wait"],
            cwd=root,
            env=environment,
            check=True,
        )
        started = True
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            summary = run_portfolio_demo(client, token=token, progress=print)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        if not summary["verified"]:
            print("[result] Demo completed without verified evidence", file=sys.stderr)
            return 1
        print("[result] Demo verified the Artifact bytes and committed Action evidence")
        return 0
    except (subprocess.CalledProcessError, DemoError, httpx.HTTPError) as exc:
        print(f"portfolio demo failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if started:
            print("[cleanup] Removing only this demo's containers, network, and disposable volumes")
            subprocess.run(
                [*compose, "down", "--volumes", "--remove-orphans"],
                cwd=root,
                env=environment,
                check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
