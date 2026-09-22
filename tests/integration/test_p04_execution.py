from __future__ import annotations

import json
import os
import pickle
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.blobs.filesystem import FileBlobStore
from hnh.adapters.execution.docker import DockerExecutionBroker
from hnh.adapters.postgres.models import (
    ActionRecord,
    ArtifactRecord,
    EventRecord,
    ExecutionRecord,
    ExecutionSessionRecord,
)
from hnh.application.action_gateway import ActionGateway, ExecutionResult
from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.execution import ExecutionService
from hnh.application.operations import HttpOperation
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.domain.errors import ResourceNotFound
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus
from hnh.ports.execution import SandboxHandle, SandboxProfile
from hnh.transport.http.app import create_app

TOKEN = "p04-token"
SCOPES = frozenset(
    {
        "actions:read",
        "artifacts:read",
        "artifacts:write",
        "capabilities:read",
        "execution:read",
        "execution:write",
        "integrations:invoke",
        "runs:read",
        "runs:write",
        "workspace:read",
        "workspace:write",
    }
)
CONTEXT = TrustedContext("tenant-p04", "subject-p04", SCOPES)
PROFILE_ID = "python-safe"


@dataclass(slots=True)
class DockerStack:
    controller: RunController
    resources: ResourceStore
    gateway: ActionGateway
    service: ExecutionService
    broker: DockerExecutionBroker
    turns: Iterator[int]


def authenticator() -> DevelopmentAuthenticator:
    return DevelopmentAuthenticator(
        {TOKEN: DevelopmentPrincipal(CONTEXT.tenant_id, CONTEXT.subject_id, SCOPES)}
    )


def headers(key: str | None = None, *, content_type: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {TOKEN}"}
    if key is not None:
        result["Idempotency-Key"] = key
    if content_type is not None:
        result["Content-Type"] = content_type
    return result


def admit_running(controller: RunController, key: str) -> str:
    run_id = controller.admit_run(
        CONTEXT,
        {
            "agent_id": "agent-p04",
            "input": "execute isolated Python",
            "limits": {"max_model_turns": 10, "max_tool_calls": 30},
        },
        key,
    ).resource_id
    controller.ensure_run_running(run_id, actor_ref="test")
    return run_id


def session_operation(run_id: str) -> HttpOperation:
    return HttpOperation(
        method="POST",
        target="/v1/execution-sessions",
        headers={"content-type": "application/json"},
        payload={
            "kind": "json",
            "value": {"run_id": run_id, "runtime": "python", "profile_id": PROFILE_ID},
        },
    )


def execution_operation(session_id: str, code: str) -> HttpOperation:
    return HttpOperation(
        method="POST",
        target=f"/v1/execution-sessions/{session_id}/executions",
        headers={"content-type": "text/plain"},
        payload={"kind": "text", "text": code},
    )


def create_session(stack: DockerStack, run_id: str, label: str) -> str:
    result = stack.gateway.execute(
        CONTEXT,
        session_operation(run_id),
        idempotency_key=f"session-{label}",
        transport_kind="inproc",
        run_id=run_id,
        turn_id=next(stack.turns),
        call_index=0,
    )
    assert result.succeeded is True
    return str(result.value["id"])


def execute_cell(stack: DockerStack, run_id: str, session_id: str, code: str) -> ExecutionResult:
    turn = next(stack.turns)
    return stack.gateway.execute(
        CONTEXT,
        execution_operation(session_id, code),
        idempotency_key=f"cell-{turn}",
        transport_kind="inproc",
        run_id=run_id,
        turn_id=turn,
        call_index=0,
    )


@pytest.fixture
def docker_stack(clean_postgres: Engine, tmp_path: Path) -> Iterator[DockerStack]:
    image = os.environ.get("HNH_TEST_SANDBOX_IMAGE")
    if not image:
        pytest.skip("blocked_environment: HNH_TEST_SANDBOX_IMAGE is required")
    profile = SandboxProfile(
        PROFILE_ID,
        image,
        cpu_limit=0.5,
        memory_bytes=64 * 1024 * 1024,
        pids_limit=32,
        timeout_seconds=1.2,
        output_bytes=4096,
        artifact_bytes=1024 * 1024,
        workspace_bytes=8 * 1024 * 1024,
    )
    broker = DockerExecutionBroker((profile,))
    controller = RunController(clean_postgres)
    resources = ResourceStore(clean_postgres, FileBlobStore(tmp_path / "blobs"))
    service = ExecutionService(controller, resources, broker)
    gateway = ActionGateway(
        clean_postgres,
        CapabilityRegistry(),
        resources,
        controller,
        service,
    )
    stack = DockerStack(controller, resources, gateway, service, broker, count(1))
    try:
        yield stack
    finally:
        service.close()
        broker.close()


def table_count(engine: Engine, model: type[Any]) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.postgres
def test_at_031_missing_broker_fails_closed_without_host_process(
    clean_postgres: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        CONTEXT,
        {"agent_id": "agent-p04", "input": "must stay isolated"},
        "at-031-run",
    ).resource_id
    calls: list[list[str]] = []

    def forbidden_popen(command: list[str], **_kwargs: Any) -> Any:
        calls.append(command)
        raise AssertionError("no host process may be started")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))
    response = client.post(
        "/v1/execution-sessions",
        headers=headers("at-031"),
        json={"run_id": run_id, "runtime": "python", "profile_id": PROFILE_ID},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "execution_unavailable"
    assert calls == []
    assert table_count(clean_postgres, ExecutionSessionRecord) == 0


@pytest.mark.docker
@pytest.mark.postgres
def test_at_032_real_sandbox_enforces_identity_and_resource_limits(
    clean_postgres: Engine,
    docker_stack: DockerStack,
) -> None:
    stack = docker_stack
    run_id = admit_running(stack.controller, "at-032")
    session_id = create_session(stack, run_id, "limits")
    session = stack.controller.get_execution_session(CONTEXT, session_id)
    assert session.sandbox_ref is not None
    handle = SandboxHandle(session.sandbox_ref, session.generation, session.profile_id)
    inspected = stack.broker.inspect(handle)
    host_config = inspected["HostConfig"]

    assert inspected["Config"]["User"] == "65534:65534"
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["CapDrop"] == ["ALL"]
    assert host_config["PidsLimit"] == 32
    assert host_config["Memory"] == 64 * 1024 * 1024
    assert host_config["NanoCpus"] == 500_000_000
    assert "no-new-privileges" in host_config["SecurityOpt"]
    assert host_config.get("Binds") in (None, [])
    assert "/workspace" in host_config["Tmpfs"]

    identity = execute_cell(stack, run_id, session_id, "import os; print(os.geteuid())")
    assert identity.value["stdout"] == "65534\n"

    flooded = execute_cell(stack, run_id, session_id, "print('x' * 10000)")
    assert flooded.succeeded is False
    assert flooded.value["status"] == "output_limit"
    assert len(flooded.value["stdout"].encode()) <= 4096
    assert stack.service.get_session(CONTEXT, session_id).status == "ready"

    timeout_session = create_session(stack, run_id, "timeout")
    timed_out = execute_cell(stack, run_id, timeout_session, "while True:\n    pass")
    assert timed_out.succeeded is False
    assert timed_out.value["status"] == "timed_out"
    assert stack.service.get_session(CONTEXT, timeout_session).status == "lost"

    memory_session = create_session(stack, run_id, "memory")
    memory = execute_cell(
        stack,
        run_id,
        memory_session,
        "value = bytearray(256 * 1024 * 1024); print(len(value))",
    )
    assert memory.succeeded is False
    assert memory.value["status"] in {"failed", "environment_lost"}

    pid_session = create_session(stack, run_id, "pids")
    pid_result = execute_cell(
        stack,
        run_id,
        pid_session,
        """
import os, time
children = []
try:
    for _ in range(100):
        pid = os.fork()
        if pid == 0:
            time.sleep(2)
            os._exit(0)
        children.append(pid)
except OSError as exc:
    print("blocked", len(children), type(exc).__name__)
""",
    )
    assert pid_result.value["status"] in {"succeeded", "timed_out"}
    if pid_result.value["status"] == "succeeded":
        assert "blocked" in pid_result.value["stdout"]
        assert int(pid_result.value["stdout"].split()[1]) < 100

    responsive_session = create_session(stack, run_id, "responsive")
    responsive = execute_cell(stack, run_id, responsive_session, "print(6 * 7)")
    assert responsive.succeeded is True
    assert responsive.value["stdout"] == "42\n"
    assert table_count(clean_postgres, ExecutionRecord) >= 6


@pytest.mark.docker
@pytest.mark.postgres
def test_at_033_sandbox_network_cannot_reach_external_metadata_or_host(
    docker_stack: DockerStack,
) -> None:
    stack = docker_stack
    run_id = admit_running(stack.controller, "at-033")
    session_id = create_session(stack, run_id, "network")
    result = execute_cell(
        stack,
        run_id,
        session_id,
        """
import json, socket
outcomes = []
for target in (("1.1.1.1", 80), ("169.254.169.254", 80), ("host.docker.internal", 80)):
    try:
        connection = socket.create_connection(target, timeout=0.25)
        connection.close()
        outcomes.append(True)
    except OSError:
        outcomes.append(False)
print(json.dumps(outcomes))
""",
    )

    assert result.succeeded is True
    assert json.loads(result.value["stdout"]) == [False, False, False]


@pytest.mark.docker
@pytest.mark.postgres
def test_at_034_persistent_python_cells_are_serial_and_import_artifacts(
    clean_postgres: Engine,
    docker_stack: DockerStack,
) -> None:
    stack = docker_stack
    run_id = admit_running(stack.controller, "at-034")
    session_id = create_session(stack, run_id, "persistent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            execute_cell,
            stack,
            run_id,
            session_id,
            "import time; time.sleep(0.3); x = 42; print('set')",
        )
        time.sleep(0.05)
        second = executor.submit(
            execute_cell,
            stack,
            run_id,
            session_id,
            (
                "from pathlib import Path; print(x + 1); "
                "Path('/workspace/outputs').mkdir(exist_ok=True); "
                "Path('/workspace/outputs/result.txt').write_text(str(x + 1))"
            ),
        )
        first_result = first.result()
        second_result = second.result()

    assert first_result.succeeded is True
    assert second_result.succeeded is True
    assert second_result.value["stdout"] == "43\n"
    assert len(second_result.value["artifacts"]) == 1
    artifact_id = second_result.value["artifacts"][0]["artifact_id"]
    content, media_type, _etag = stack.resources.get_artifact_content(CONTEXT, artifact_id)
    assert content == b"43"
    assert media_type == "text/plain"
    with Session(clean_postgres) as session:
        executions = session.scalars(
            select(ExecutionRecord)
            .where(ExecutionRecord.session_id == session_id)
            .order_by(ExecutionRecord.cell_index)
        ).all()
        assert [item.cell_index for item in executions] == [1, 2]
        artifact = session.get(ArtifactRecord, artifact_id)
        assert artifact is not None
        assert artifact.content is None
        assert artifact.storage_key is not None


@pytest.mark.docker
@pytest.mark.postgres
def test_at_035_recreated_session_has_new_generation_and_no_old_variables(
    clean_postgres: Engine,
    docker_stack: DockerStack,
) -> None:
    stack = docker_stack
    run_id = admit_running(stack.controller, "at-035")
    session_id = create_session(stack, run_id, "generation")
    initial = stack.service.get_session(CONTEXT, session_id)
    assert initial.sandbox_ref is not None
    first = execute_cell(stack, run_id, session_id, "x = 42")
    assert first.succeeded is True
    stack.broker.stop(SandboxHandle(initial.sandbox_ref, initial.generation, initial.profile_id))

    lost = stack.service.reconcile_session(CONTEXT, session_id)
    assert lost.status == "lost"
    recreated = stack.service.replace_session(CONTEXT, session_id)
    assert recreated.generation != initial.generation
    missing = execute_cell(stack, run_id, session_id, "print(x)")

    assert missing.succeeded is False
    assert missing.value["status"] == "failed"
    assert missing.value["error"]["type"] == "NameError"
    with Session(clean_postgres) as session:
        generations = session.scalars(
            select(ExecutionRecord.generation)
            .where(ExecutionRecord.session_id == session_id)
            .order_by(ExecutionRecord.cell_index)
        ).all()
        assert generations == [initial.generation, recreated.generation]
        lost_event = session.scalar(
            select(EventRecord.id).where(
                EventRecord.run_id == run_id,
                EventRecord.event_type == "execution.session_lost",
            )
        )
        assert lost_event is not None


@pytest.mark.docker
@pytest.mark.postgres
def test_at_036_transport_success_does_not_mask_python_exception(
    clean_postgres: Engine,
    docker_stack: DockerStack,
) -> None:
    stack = docker_stack
    run_id = admit_running(stack.controller, "at-036")
    session_id = create_session(stack, run_id, "exception")
    result = execute_cell(stack, run_id, session_id, "raise ValueError('boom')")

    assert result.succeeded is False
    assert result.status_code == 200
    assert result.value["transport_ok"] is True
    assert result.value["status"] == "failed"
    assert result.value["error"] == {"type": "ValueError", "message": "boom"}
    with Session(clean_postgres) as session:
        action = session.get(ActionRecord, result.action_id)
        execution = session.scalar(
            select(ExecutionRecord).where(ExecutionRecord.action_id == result.action_id)
        )
        assert action is not None and action.status == ActionStatus.FAILED.value
        assert execution is not None and execution.status == "failed"


@pytest.mark.docker
@pytest.mark.postgres
def test_execution_http_surface_returns_session_and_action_handles(
    clean_postgres: Engine,
    docker_stack: DockerStack,
    tmp_path: Path,
) -> None:
    stack = docker_stack
    run_id = stack.controller.admit_run(
        CONTEXT,
        {"agent_id": "agent-p04", "input": "exercise the HTTP execution surface"},
        "p04-http-run",
    ).resource_id
    app = create_app(
        engine=clean_postgres,
        authenticator=authenticator(),
        execution_broker=stack.broker,
        blob_store=FileBlobStore(tmp_path / "http-blobs"),
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/execution-sessions",
            headers=headers("p04-http-session"),
            json={"run_id": run_id, "runtime": "python", "profile_id": PROFILE_ID},
        )
        assert created.status_code == 202
        session_id = created.json()["resource_id"]
        assert created.headers["location"] == f"/v1/execution-sessions/{session_id}"

        inspected = client.get(
            f"/v1/execution-sessions/{session_id}",
            headers=headers(),
        )
        assert inspected.status_code == 200
        assert inspected.json()["status"] == "ready"

        executed = client.post(
            f"/v1/execution-sessions/{session_id}/executions",
            headers=headers("p04-http-cell", content_type="text/plain"),
            content="print(40 + 2)",
        )
        assert executed.status_code == 202
        action_id = executed.json()["resource_id"]
        assert executed.headers["location"] == f"/v1/actions/{action_id}"

        action = client.get(f"/v1/actions/{action_id}", headers=headers())
        assert action.status_code == 200
        assert action.json()["status"] == "succeeded"
        assert action.json()["result"]["value"]["stdout"] == "42\n"


@pytest.mark.postgres
def test_at_037_blob_before_metadata_failure_leaves_only_collectable_orphan(
    clean_postgres: Engine,
    tmp_path: Path,
) -> None:
    blobs = FileBlobStore(tmp_path / "artifact-store")
    resources = ResourceStore(clean_postgres, blobs)

    def crash_before_metadata() -> None:
        raise RuntimeError("kill after blob publication")

    with pytest.raises(RuntimeError, match="after blob"):
        resources.create_artifact(
            CONTEXT,
            b"orphan bytes",
            "application/octet-stream",
            "at-037-orphan",
            after_blob_before_metadata=crash_before_metadata,
        )

    orphan_keys = blobs.list_keys()
    assert len(orphan_keys) == 1
    assert table_count(clean_postgres, ArtifactRecord) == 0
    with pytest.raises(ResourceNotFound):
        resources.get_artifact(CONTEXT, "artifact_missing")
    assert set(resources.garbage_collect_blobs()) == orphan_keys
    assert blobs.list_keys() == set()

    artifact = resources.create_artifact(
        CONTEXT,
        b"committed bytes",
        "application/octet-stream",
        "at-037-committed",
    )
    assert resources.get_artifact_content(CONTEXT, artifact.id)[0] == b"committed bytes"
    assert resources.garbage_collect_blobs() == ()


@pytest.mark.postgres
def test_at_038_pickle_payload_is_rejected_before_deserialization(
    clean_postgres: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("pickle")
        raise AssertionError("pickle deserialization must not run")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))
    response = client.post(
        "/v1/execution-sessions/session_missing/executions",
        headers=headers("at-038", content_type="application/x-python-pickle"),
        content=b"cos\nsystem\n(S'echo unsafe'\ntR.",
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"
    assert calls == []
    assert table_count(clean_postgres, ActionRecord) == 0
