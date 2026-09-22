"""The opt-in dev worker processes an API-admitted Run in a separate composition."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from hnh.adapters.decision.typesafe import TypeSafeDecisionProvider
from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import ArtifactRecord
from hnh.application.run_controller import RunController
from hnh.config import Settings
from hnh.domain.states import RunStatus
from hnh.transport.http.app import create_app
from hnh.worker_dev import build_development_worker


@pytest.mark.postgres
def test_dev_worker_advances_api_admitted_run_with_same_configured_principal(
    clean_postgres: Engine,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://example.invalid/development",
        development_token="only-local-dev-token",
        development_tenant="tenant-p08-dev",
        development_subject="subject-p08-dev",
    )
    actor = settings.authenticator().authenticate("Bearer only-local-dev-token", "runs:read")
    with TestClient(create_app(settings=settings, engine=clean_postgres)) as client:
        accepted = client.post(
            "/v1/runs",
            headers={
                "Authorization": "Bearer only-local-dev-token",
                "Idempotency-Key": "p08-dev-worker-accept",
            },
            json={
                "agent_id": "p08-dev",
                "input": "complete using committed admission evidence",
                "limits": {"max_model_turns": 1, "max_output_tokens": 1024},
            },
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["resource_id"]
        assert (
            client.get(
                f"/v1/runs/{run_id}", headers={"Authorization": "Bearer only-local-dev-token"}
            ).json()["status"]
            == "queued"
        )

        controller = RunController(clean_postgres)
        evidence_id = controller.get_recent_events(actor, run_id)[0].event_id
        provider = ScriptedProvider(
            [
                {
                    "final_candidate": {
                        "answer": "The accepted Run exists.",
                        "evidence_ids": [evidence_id],
                        "acceptance_claims": ["admission event was committed"],
                    }
                }
            ]
        )
        worker = build_development_worker(
            settings, engine=clean_postgres, provider=provider, worker_id="dev-worker-test"
        )
        step = worker.run_once()
        assert step is not None and step.run_id == run_id and step.disposition == "succeeded"
        assert provider.calls == 1
        assert worker.run_once() is None
        assert controller.get_run(actor, run_id).status == RunStatus.SUCCEEDED
        assert (
            client.get(
                f"/v1/runs/{run_id}", headers={"Authorization": "Bearer only-local-dev-token"}
            ).json()["status"]
            == "succeeded"
        )


def test_dev_worker_refuses_missing_configuration() -> None:
    with pytest.raises(ValueError, match=r"HNH_DATABASE_URL.*HNH_DEV_TOKEN.*HNH_DEEPSEEK_API_KEY"):
        build_development_worker(Settings(database_url=None, development_token=None))


@pytest.mark.postgres
def test_dev_worker_wires_enabled_typesafe_as_optional_decision_provider(
    clean_postgres: Engine,
) -> None:
    settings = Settings(
        database_url=clean_postgres.url.render_as_string(hide_password=False),
        development_token="typesafe-wiring-token",
        typesafe_enabled=True,
        typesafe_api_key="test-only-typesafe-secret",
        typesafe_timeout_seconds=3.0,
        typesafe_min_confidence=0.85,
    )
    worker = build_development_worker(
        settings,
        engine=clean_postgres,
        provider=ScriptedProvider([]),
        worker_id="typesafe-wiring-worker",
    )
    assert worker.runner is not None
    router = worker.runner.decision_router
    assert isinstance(router.provider, TypeSafeDecisionProvider)
    assert router.timeout_seconds == 3.0
    assert router.min_confidence == 0.85


@pytest.mark.postgres
def test_dev_worker_confirms_api_requested_cancellation_without_model_call(
    clean_postgres: Engine,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://example.invalid/development",
        development_token="cancel-local-dev-token",
        development_tenant="tenant-p08-cancel-api",
        development_subject="subject-p08-cancel-api",
    )
    provider = ScriptedProvider([])
    with TestClient(create_app(settings=settings, engine=clean_postgres)) as client:
        accepted = client.post(
            "/v1/runs",
            headers={
                "Authorization": "Bearer cancel-local-dev-token",
                "Idempotency-Key": "p08-dev-cancel-accept",
            },
            json={"agent_id": "p08-dev-cancel", "input": "cancel before model work"},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["resource_id"]
        requested = client.post(
            f"/v1/runs/{run_id}/cancellations",
            headers={
                "Authorization": "Bearer cancel-local-dev-token",
                "Idempotency-Key": "p08-dev-cancel-request",
            },
            json={"reason": "user stop"},
        )
        assert requested.status_code == 202
        assert (
            client.get(
                f"/v1/runs/{run_id}",
                headers={"Authorization": "Bearer cancel-local-dev-token"},
            ).json()["status"]
            == "cancelling"
        )
        worker = build_development_worker(
            settings, engine=clean_postgres, provider=provider, worker_id="dev-cancel-api"
        )
        step = worker.run_once()
        assert step is not None and step.run_id == run_id and step.disposition == "cancelled"
        assert provider.calls == 0
        assert (
            client.get(
                f"/v1/runs/{run_id}",
                headers={"Authorization": "Bearer cancel-local-dev-token"},
            ).json()["status"]
            == "cancelled"
        )


@pytest.mark.postgres
def test_cancel_only_cli_does_not_need_model_or_claim_ordinary_run(clean_postgres: Engine) -> None:
    settings = Settings(
        database_url=clean_postgres.url.render_as_string(hide_password=False),
        development_token="cancel-only-local-token",
        development_tenant="tenant-p08-cancel-only",
        development_subject="subject-p08-cancel-only",
    )
    actor = settings.authenticator().authenticate("Bearer cancel-only-local-token", "runs:read")
    headers = {"Authorization": "Bearer cancel-only-local-token"}
    with TestClient(create_app(settings=settings, engine=clean_postgres)) as client:
        queued = client.post(
            "/v1/runs",
            headers={**headers, "Idempotency-Key": "p08-cancel-only-queued"},
            json={"agent_id": "p08-cancel-only", "input": "ordinary work remains queued"},
        )
        assert queued.status_code == 202
        queued_id = queued.json()["resource_id"]
        requested = client.post(
            "/v1/runs",
            headers={**headers, "Idempotency-Key": "p08-cancel-only-target"},
            json={"agent_id": "p08-cancel-only", "input": "stop this work"},
        )
        assert requested.status_code == 202
        cancel_id = requested.json()["resource_id"]
        assert (
            client.post(
                f"/v1/runs/{cancel_id}/cancellations",
                headers={**headers, "Idempotency-Key": "p08-cancel-only-request"},
                json={"reason": "user stop"},
            ).status_code
            == 202
        )

    environment = {name: value for name, value in os.environ.items() if not name.startswith("HNH_")}
    environment.update(
        {
            "HNH_DATABASE_URL": settings.database_url or "",
            "HNH_DEV_TOKEN": settings.development_token or "",
            "HNH_DEV_TENANT": settings.development_tenant,
            "HNH_DEV_SUBJECT": settings.development_subject,
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "hnh.worker_dev", "--cancel-only", "--once"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    controller = RunController(clean_postgres)
    assert controller.get_run(actor, cancel_id).status == RunStatus.CANCELLED
    assert controller.get_run(actor, queued_id).status == RunStatus.QUEUED
    assert controller.latest_model_call(actor, cancel_id) is None
    assert controller.latest_model_call(actor, queued_id) is None


@pytest.mark.postgres
def test_dev_worker_artifact_is_readable_from_api_shared_blob_root(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    settings = Settings(
        database_url=clean_postgres.url.render_as_string(hide_password=False),
        development_token="blob-local-dev-token",
        development_tenant="tenant-p08-shared-blob",
        development_subject="subject-p08-shared-blob",
        blob_root=str(tmp_path / "shared-blobs"),
    )
    provider = ScriptedProvider(
        [
            {
                "operations": [
                    {
                        "method": "POST",
                        "target": "/v1/artifacts",
                        "headers": {"content-type": "application/octet-stream"},
                        "payload": {
                            "kind": "json",
                            "value": {
                                "content_base64": base64.b64encode(
                                    b"shared worker artifact"
                                ).decode(),
                                "media_type": "text/plain",
                            },
                        },
                    }
                ]
            }
        ]
    )
    auth = {"Authorization": "Bearer blob-local-dev-token"}
    with TestClient(create_app(settings=settings, engine=clean_postgres)) as client:
        accepted = client.post(
            "/v1/runs",
            headers={**auth, "Idempotency-Key": "p08-shared-blob-run"},
            json={"agent_id": "p08-shared-blob", "input": "store an artifact"},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["resource_id"]
        worker = build_development_worker(
            settings, engine=clean_postgres, provider=provider, worker_id="dev-shared-blob"
        )
        step = worker.run_once()
        assert step is not None and step.run_id == run_id and step.disposition == "deferred"
        actor = settings.authenticator().authenticate(auth["Authorization"], "runs:read")
        action = RunController(clean_postgres).list_actions(actor, run_id)[0]
        assert action.result is not None
        artifact_id = action.result["value"]["id"]
        response = client.get(f"/v1/artifacts/{artifact_id}/content", headers=auth)
        assert response.status_code == 200 and response.content == b"shared worker artifact"
    with Session(clean_postgres) as session:
        record = session.scalar(select(ArtifactRecord).where(ArtifactRecord.id == artifact_id))
        assert record is not None and record.storage_key is not None
        assert record.content is None


@pytest.mark.postgres
def test_dev_worker_cli_process_completes_api_run_against_local_model_fixture(
    clean_postgres: Engine,
) -> None:
    settings = Settings(
        database_url=clean_postgres.url.render_as_string(hide_password=False),
        development_token="process-local-dev-token",
        development_tenant="tenant-p08-process",
        development_subject="subject-p08-process",
    )
    actor = settings.authenticator().authenticate("Bearer process-local-dev-token", "runs:read")
    with TestClient(create_app(settings=settings, engine=clean_postgres)) as client:
        accepted = client.post(
            "/v1/runs",
            headers={
                "Authorization": "Bearer process-local-dev-token",
                "Idempotency-Key": "p08-process-worker",
            },
            json={"agent_id": "p08-process", "input": "finish with acceptance event"},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["resource_id"]
    controller = RunController(clean_postgres)
    evidence_id = controller.get_recent_events(actor, run_id)[0].event_id
    response_text = json.dumps(
        {
            "public_output": "Done.",
            "operations": [],
            "final_candidate": {
                "answer": "Done.",
                "artifact_ids": [],
                "evidence_ids": [evidence_id],
                "acceptance_claims": ["admission event exists"],
            },
            "request_input": None,
        }
    )
    requests: list[str] = []

    class ModelFixture(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            requests.append(self.path)
            payload = json.dumps(
                {
                    "id": "resp-p08-process",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": response_text}],
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("HNH_")
        }
        environment.update(
            {
                "HNH_DATABASE_URL": settings.database_url or "",
                "HNH_DEV_TOKEN": settings.development_token or "",
                "HNH_DEV_TENANT": settings.development_tenant,
                "HNH_DEV_SUBJECT": settings.development_subject,
                "HNH_DEEPSEEK_API_KEY": "fixture-not-a-secret",
                "HNH_DEEPSEEK_MODEL": "deepseek-flash",
                "HNH_DEEPSEEK_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "HNH_DEEPSEEK_REASONING_EFFORT": "high",
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "hnh.worker_dev", "--once"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
    assert result.returncode == 0, result.stderr
    assert requests == ["/v1/responses"]
    assert controller.get_run(actor, run_id).status == RunStatus.SUCCEEDED
