"""Recording-friendly client for the disposable local portfolio demo.

The client talks only to the public HTTP API.  It does not import the kernel,
write PostgreSQL directly, or treat a 202 response as task completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx

TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})
DEMO_INPUT = b"paper boat\n"
DEMO_EXPECTED = b"PAPER BOAT\n"


class DemoError(RuntimeError):
    """A safe operator-facing demo failure without response bodies or secrets."""


def _expect(response: httpx.Response, *statuses: int) -> httpx.Response:
    if response.status_code not in statuses:
        raise DemoError(
            f"{response.request.method} {response.request.url.path} returned "
            f"HTTP {response.status_code}"
        )
    return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise DemoError(
            f"{response.request.method} {response.request.url.path} returned invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise DemoError(
            f"{response.request.method} {response.request.url.path} returned non-object JSON"
        )
    return value


def run_portfolio_demo(
    client: httpx.Client,
    *,
    token: str,
    timeout_seconds: float = 180.0,
    poll_seconds: float = 1.0,
    demo_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one HTTP -> model -> Action -> Artifact -> evidence demonstration."""
    if not token:
        raise ValueError("token is required")
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("demo timeout and poll interval must be positive")
    announce = progress or (lambda _message: None)
    identifier = demo_id or uuid4().hex[:12]
    workspace_id = f"portfolio-{identifier}"
    file_path = "input.txt"
    auth = {"Authorization": f"Bearer {token}"}

    announce("[1/5] Checking database-backed API readiness")
    readiness = _json_object(_expect(client.get("/readyz"), 200))

    announce("[2/5] Writing the managed input file through the HTTP resource API")
    _expect(
        client.put(
            f"/v1/workspaces/{workspace_id}/files/{file_path}",
            headers={
                **auth,
                "Content-Type": "text/plain",
                "If-None-Match": "*",
                "Idempotency-Key": f"portfolio-seed-{identifier}",
            },
            content=DEMO_INPUT,
        ),
        200,
        201,
    )

    target = f"/v1/workspaces/{workspace_id}/files/{file_path}"
    goal = (
        f"Read the exact contents of {target}. After that read result is committed and visible "
        "in a later turn, convert every letter to uppercase while preserving the final LF. "
        "Create one immutable text/plain Artifact containing exactly those UTF-8 bytes. "
        "Finish only after the Artifact create result is committed, citing the Artifact ID and "
        "the committed read/create Action IDs. Do not combine dependent read and create "
        "operations in one decision, and do not invent observations or identifiers."
    )
    announce("[3/5] Admitting a durable Run (HTTP 202 means accepted, not completed)")
    accepted = _json_object(
        _expect(
            client.post(
                "/v1/runs",
                headers={**auth, "Idempotency-Key": f"portfolio-run-{identifier}"},
                json={
                    "agent_id": "portfolio-demo",
                    "input": goal,
                    "workspace_id": workspace_id,
                    "limits": {
                        "max_model_turns": 8,
                        "max_tool_calls": 8,
                        "max_output_tokens": 8192,
                        "max_wall_seconds": max(1, int(timeout_seconds)),
                    },
                },
            ),
            202,
        )
    )
    run_id = accepted.get("resource_id")
    if not isinstance(run_id, str) or not run_id:
        raise DemoError("Run admission response did not contain a resource_id")

    announce(f"[4/5] Following committed Run state: {run_id}")
    deadline = clock() + timeout_seconds
    last_status: str | None = None
    run: dict[str, Any]
    while True:
        run = _json_object(_expect(client.get(f"/v1/runs/{run_id}", headers=auth), 200))
        status = run.get("status")
        if not isinstance(status, str):
            raise DemoError("Run response did not contain a status")
        if status != last_status:
            announce(f"      status={status}")
            last_status = status
        if status in TERMINAL_RUN_STATUSES or status in {"blocked", "waiting_input"}:
            break
        if clock() >= deadline:
            raise DemoError(f"Run {run_id} did not finish within {timeout_seconds:g} seconds")
        sleep(poll_seconds)

    history = _json_object(
        _expect(
            client.get(f"/v1/runs/{run_id}/event-history?after=0&limit=1000", headers=auth), 200
        )
    )
    events = history.get("events")
    if not isinstance(events, list):
        raise DemoError("Event history response did not contain an events list")
    action_ids = sorted(
        {
            event["action_id"]
            for event in events
            if isinstance(event, dict) and isinstance(event.get("action_id"), str)
        }
    )
    event_types = [
        event["type"]
        for event in events
        if isinstance(event, dict) and isinstance(event.get("type"), str)
    ]

    artifact_ids = run.get("artifact_ids")
    if not isinstance(artifact_ids, list) or not all(
        isinstance(item, str) for item in artifact_ids
    ):
        raise DemoError("Run response contained invalid artifact identifiers")
    artifact_id = artifact_ids[0] if len(artifact_ids) == 1 else None
    content: bytes | None = None
    media_type: str | None = None
    if artifact_id is not None:
        artifact = _expect(client.get(f"/v1/artifacts/{artifact_id}/content", headers=auth), 200)
        content = artifact.content
        media_type = artifact.headers.get("content-type", "").split(";", 1)[0]

    verified = (
        run.get("status") == "succeeded"
        and artifact_id is not None
        and content == DEMO_EXPECTED
        and media_type == "text/plain"
        and len(action_ids) >= 2
    )
    announce("[5/5] Verifying Artifact bytes and committed Action evidence")
    summary = {
        "verified": verified,
        "run_id": run_id,
        "run_status": run.get("status"),
        "final_answer": run.get("final_answer"),
        "failure": run.get("failure"),
        "artifact_id": artifact_id,
        "artifact_media_type": media_type,
        "artifact_sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
        "expected_sha256": hashlib.sha256(DEMO_EXPECTED).hexdigest(),
        "action_ids": action_ids,
        "event_count": len(events),
        "event_types": event_types,
        "readiness_status": readiness.get("status"),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the recording-friendly HTTP-native Harness portfolio demo"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("HNH_DEMO_BASE_URL", "http://127.0.0.1:8080"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    token = os.environ.get("HNH_DEV_TOKEN")
    if not token:
        print("portfolio demo configuration missing: HNH_DEV_TOKEN", file=sys.stderr)
        return 2
    try:
        with httpx.Client(
            base_url=args.base_url,
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            summary = run_portfolio_demo(
                client,
                token=token,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                progress=print,
            )
    except (DemoError, httpx.HTTPError) as exc:
        print(f"portfolio demo failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
