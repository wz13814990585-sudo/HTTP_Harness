from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from hnh.demo import DEMO_EXPECTED, run_portfolio_demo


def test_portfolio_demo_verifies_http_run_artifact_and_action_evidence() -> None:
    run_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal run_reads
        path = request.url.path
        if request.method == "GET" and path == "/readyz":
            return httpx.Response(200, json={"status": "core_ready"})
        if request.method == "PUT" and path.endswith("/files/input.txt"):
            assert request.content == b"paper boat\n"
            assert request.headers["if-none-match"] == "*"
            return httpx.Response(201, json={"revision": 1})
        if request.method == "POST" and path == "/v1/runs":
            body = json.loads(request.content)
            assert "202" not in body["input"]
            assert "Do not combine dependent read and create" in body["input"]
            return httpx.Response(
                202,
                json={"resource_id": "run_demo", "location": "/v1/runs/run_demo"},
            )
        if request.method == "GET" and path == "/v1/runs/run_demo":
            run_reads += 1
            if run_reads == 1:
                return httpx.Response(
                    200, json={"id": "run_demo", "status": "queued", "artifact_ids": []}
                )
            return httpx.Response(
                200,
                json={
                    "id": "run_demo",
                    "status": "succeeded",
                    "artifact_ids": ["artifact_demo"],
                    "final_answer": "Done",
                    "failure": None,
                },
            )
        if request.method == "GET" and path == "/v1/runs/run_demo/event-history":
            events: list[dict[str, Any]] = [
                {"type": "action.succeeded", "action_id": "action_read"},
                {"type": "action.succeeded", "action_id": "action_create"},
                {"type": "run.succeeded", "action_id": None},
            ]
            return httpx.Response(200, json={"events": events, "next_after": None})
        if request.method == "GET" and path == "/v1/artifacts/artifact_demo/content":
            return httpx.Response(
                200,
                content=DEMO_EXPECTED,
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(
        base_url="http://demo.test",
        transport=httpx.MockTransport(respond),
    ) as client:
        result = run_portfolio_demo(
            client,
            token="secret-not-printed",
            demo_id="unit",
            poll_seconds=0.01,
            sleep=lambda _seconds: None,
        )

    assert result["verified"] is True
    assert result["action_ids"] == ["action_create", "action_read"]
    assert result["artifact_sha256"] == hashlib.sha256(DEMO_EXPECTED).hexdigest()


def test_portfolio_demo_preserves_a_failed_run_as_failure() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/readyz":
            return httpx.Response(200, json={"status": "core_ready"})
        if request.method == "PUT":
            return httpx.Response(201, json={})
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"resource_id": "run_failed", "location": "/v1/runs/run_failed"},
            )
        if path == "/v1/runs/run_failed":
            return httpx.Response(
                200,
                json={
                    "id": "run_failed",
                    "status": "failed",
                    "artifact_ids": [],
                    "final_answer": None,
                    "failure": {"code": "model_output_invalid"},
                },
            )
        if path == "/v1/runs/run_failed/event-history":
            return httpx.Response(200, json={"events": [], "next_after": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(
        base_url="http://demo.test",
        transport=httpx.MockTransport(respond),
    ) as client:
        result = run_portfolio_demo(client, token="secret", demo_id="failed")

    assert result["verified"] is False
    assert result["run_status"] == "failed"
    assert result["failure"] == {"code": "model_output_invalid"}
