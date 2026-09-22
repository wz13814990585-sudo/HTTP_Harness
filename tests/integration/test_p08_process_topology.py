"""Single-node API/worker processes share PostgreSQL; model HTTP is a fixture."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic, sleep

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.postgres.models import ActionRecord, ModelCallAttemptRecord
from hnh.application.run_controller import RunController
from hnh.config import Settings
from hnh.domain.states import RunStatus


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.postgres
def test_api_and_worker_processes_finish_a_run_over_real_http(clean_postgres: Engine) -> None:
    answer: dict[str, str] = {}
    model_requests: list[str] = []

    class ModelFixture(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", "0")))
            model_requests.append(self.path)
            turn = {
                "public_output": "Done.",
                "operations": [],
                "final_candidate": {
                    "answer": "Done.",
                    "artifact_ids": [],
                    "evidence_ids": [answer["evidence_id"]],
                    "acceptance_claims": ["admission event exists"],
                },
                "request_input": None,
            }
            response = json.dumps(
                {
                    "id": "resp-process-topology",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(turn)}],
                        }
                    ],
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    model_thread = Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    port = _available_port()
    token = "local-topology-token"
    environment = {name: value for name, value in os.environ.items() if not name.startswith("HNH_")}
    environment.update(
        {
            "HNH_DATABASE_URL": clean_postgres.url.render_as_string(hide_password=False),
            "HNH_DEV_TOKEN": token,
            "HNH_DEV_TENANT": "tenant-p08-topology",
            "HNH_DEV_SUBJECT": "subject-p08-topology",
            "HNH_OPENAI_API_KEY": "fixture-not-a-secret",
            "HNH_OPENAI_MODEL": "fixture-model",
            "HNH_OPENAI_BASE_URL": f"http://127.0.0.1:{model_server.server_port}/v1",
        }
    )
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "hnh.transport.http.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
            deadline = monotonic() + 10
            while True:
                if api.poll() is not None:
                    raise AssertionError("API process exited before becoming healthy")
                try:
                    if client.get("/healthz").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                if monotonic() >= deadline:
                    raise AssertionError("API process did not become healthy")
                sleep(0.1)
            accepted = client.post(
                "/v1/runs",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "p08-real-process-topology",
                },
                json={"agent_id": "p08-topology", "input": "finish with admission evidence"},
            )
            assert accepted.status_code == 202
            run_id = accepted.json()["resource_id"]
            assert (
                client.get(
                    f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
                ).json()["status"]
                == "queued"
            )
            controller = RunController(clean_postgres)
            actor = (
                Settings(
                    database_url=clean_postgres.url.render_as_string(hide_password=False),
                    development_token=token,
                    development_tenant="tenant-p08-topology",
                    development_subject="subject-p08-topology",
                )
                .authenticator()
                .authenticate(f"Bearer {token}", "runs:read")
            )
            answer["evidence_id"] = controller.get_recent_events(actor, run_id)[0].event_id
            worker = subprocess.run(
                [sys.executable, "-m", "hnh.worker_dev", "--once"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert worker.returncode == 0, worker.stderr
            assert model_requests == ["/v1/responses"]
            assert controller.get_run(actor, run_id).status == RunStatus.SUCCEEDED
            assert (
                client.get(
                    f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
                ).json()["status"]
                == "succeeded"
            )
    finally:
        api.terminate()
        try:
            api.wait(timeout=5)
        except subprocess.TimeoutExpired:
            api.kill()
            api.wait(timeout=5)
        model_server.shutdown()
        model_thread.join(timeout=5)
        model_server.server_close()


@pytest.mark.postgres
def test_killed_worker_model_call_is_reclaimed_without_action_replay(
    clean_postgres: Engine,
) -> None:
    first_entered = Event()
    release_first = Event()
    requests: list[str] = []
    response_text: dict[str, str] = {}

    class PausedModelFixture(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", "0")))
            requests.append(self.path)
            if len(requests) == 1:
                first_entered.set()
                release_first.wait(10)
            response = json.dumps(
                {
                    "id": "resp-after-reclaim",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": response_text["value"]}],
                        }
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The first worker was deliberately terminated.

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), PausedModelFixture)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    token = "local-chaos-token"
    settings = Settings(
        database_url=clean_postgres.url.render_as_string(hide_password=False),
        development_token=token,
        development_tenant="tenant-p08-chaos",
        development_subject="subject-p08-chaos",
    )
    actor = settings.authenticator().authenticate(f"Bearer {token}", "runs:read")
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        actor,
        {"agent_id": "p08-chaos", "input": "recover a lost model response"},
        "p08-killed-worker",
    ).resource_id
    evidence_id = controller.get_recent_events(actor, run_id)[0].event_id
    response_text["value"] = json.dumps(
        {
            "public_output": "Recovered.",
            "operations": [],
            "final_candidate": {
                "answer": "Recovered.",
                "artifact_ids": [],
                "evidence_ids": [evidence_id],
                "acceptance_claims": ["admission event exists"],
            },
            "request_input": None,
        }
    )
    environment = {name: value for name, value in os.environ.items() if not name.startswith("HNH_")}
    environment.update(
        {
            "HNH_DATABASE_URL": settings.database_url or "",
            "HNH_DEV_TOKEN": token,
            "HNH_DEV_TENANT": settings.development_tenant,
            "HNH_DEV_SUBJECT": settings.development_subject,
            "HNH_OPENAI_API_KEY": "fixture-not-a-secret",
            "HNH_OPENAI_MODEL": "fixture-model",
            "HNH_OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        }
    )
    first = subprocess.Popen(
        [sys.executable, "-m", "hnh.worker_dev", "--once", "--lease-seconds", "3"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert first_entered.wait(5)
        started = controller.latest_model_call(actor, run_id)
        assert started is not None and started.status == "started"
        first.terminate()
        first.wait(timeout=5)
        release_first.set()
        sleep(3.4)  # The dead worker cannot heartbeat its three-second lease.
        second = subprocess.run(
            [sys.executable, "-m", "hnh.worker_dev", "--once", "--lease-seconds", "3"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert second.returncode == 0, second.stderr
        assert requests == ["/v1/responses", "/v1/responses"]
        assert controller.get_run(actor, run_id).status == RunStatus.SUCCEEDED
        with Session(clean_postgres) as session:
            assert session.scalar(select(func.count()).select_from(ActionRecord)) == 0
            assert session.scalar(select(func.count()).select_from(ModelCallAttemptRecord)) == 2
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
        release_first.set()
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
