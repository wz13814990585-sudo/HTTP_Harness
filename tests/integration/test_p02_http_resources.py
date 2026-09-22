from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.postgres.models import (
    ActionRecord,
    ArtifactRecord,
    HttpExchangeRecord,
    JobRecord,
    WorkspaceEntryRecord,
)
from hnh.application.action_gateway import ActionGateway
from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.operations import HttpOperation
from hnh.application.resources import normalize_file_path
from hnh.application.run_controller import RunController
from hnh.domain.errors import InvalidOperation, PermissionDenied, SchemaInvalid
from hnh.domain.identity import TrustedContext
from hnh.domain.states import RunStatus
from hnh.transport.http.app import create_app

TOKEN_FULL = "p02-full"
TOKEN_READ = "p02-read"
FULL_SCOPES = frozenset(
    {
        "actions:read",
        "artifacts:read",
        "artifacts:write",
        "capabilities:read",
        "integrations:invoke",
        "runs:read",
        "runs:write",
        "workspace:read",
        "workspace:write",
    }
)
READ_SCOPES = frozenset({"capabilities:read", "workspace:read", "artifacts:read"})
CONTEXT = TrustedContext("tenant-p02", "subject-full", FULL_SCOPES)


def p02_authenticator() -> DevelopmentAuthenticator:
    return DevelopmentAuthenticator(
        {
            TOKEN_FULL: DevelopmentPrincipal("tenant-p02", "subject-full", FULL_SCOPES),
            TOKEN_READ: DevelopmentPrincipal("tenant-p02", "subject-read", READ_SCOPES),
        }
    )


def request_headers(token: str = TOKEN_FULL, key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def count(engine: Engine, model: type[Any]) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def echo_operation(message: Any = "hello") -> HttpOperation:
    return HttpOperation(
        method="POST",
        target="/v1/integrations/native/operations/echo/invocations",
        headers={"content-type": "application/json"},
        payload={"kind": "json", "value": {"message": message}},
    )


@pytest.mark.postgres
def test_at_012_catalog_pagination_is_stable_and_subject_filtered(
    clean_postgres: Engine,
) -> None:
    del clean_postgres
    registry = CapabilityRegistry()
    full_pages: list[str] = []
    cursor: str | None = None
    revisions: set[str] = set()
    while True:
        page = registry.list(CONTEXT, cursor, 2)
        revisions.add(page.catalog_revision)
        full_pages.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    repeated = registry.list(CONTEXT, None, 100)
    read_context = TrustedContext("tenant-p02", "subject-read", READ_SCOPES)
    read_only = registry.list(read_context, None, 100)

    assert full_pages == sorted(full_pages)
    assert len(full_pages) == len(set(full_pages))
    assert full_pages == [item.id for item in repeated.items]
    assert len(revisions) == 1
    assert {item.id for item in read_only.items} == {
        "artifact.content.read",
        "artifact.read",
        "workspace.file.read",
    }
    with pytest.raises(InvalidOperation, match="cursor"):
        registry.list(CONTEXT, "W10", 2)  # base64url for JSON []


@pytest.mark.postgres
def test_at_013_catalog_cache_is_identity_scoped_and_dispatch_reauthorizes(
    clean_postgres: Engine,
) -> None:
    auth = p02_authenticator()
    app = create_app(engine=clean_postgres, authenticator=auth)
    client = TestClient(app)

    full = client.get("/v1/capabilities", headers=request_headers(TOKEN_FULL))
    limited = client.get("/v1/capabilities", headers=request_headers(TOKEN_READ))
    assert full.status_code == limited.status_code == 200
    assert len(full.json()["items"]) > len(limited.json()["items"])
    cache_keys = app.state.capability_registry.cache_keys
    assert {key[1] for key in cache_keys} == {"subject-full", "subject-read"}

    auth.set_scopes(TOKEN_FULL, FULL_SCOPES - {"workspace:write"})
    denied = client.put(
        "/v1/workspaces/ws/files/report.txt",
        headers={
            **request_headers(TOKEN_FULL, "revoked-write"),
            "If-None-Match": "*",
            "Content-Type": "text/plain",
        },
        content="must not be written",
    )
    assert denied.status_code == 403
    assert count(clean_postgres, WorkspaceEntryRecord) == 0
    with pytest.raises(PermissionDenied):
        auth.authenticate(f"Bearer {TOKEN_FULL}", "workspace:write")


@pytest.mark.postgres
def test_at_014_absolute_targets_and_identity_forgery_fail_before_dispatch(
    clean_postgres: Engine,
) -> None:
    app = create_app(engine=clean_postgres, authenticator=p02_authenticator())
    gateway: ActionGateway = app.state.action_gateway
    attacks = [
        {"method": "GET", "target": "http://169.254.169.254/latest/meta-data"},
        {
            "method": "GET",
            "target": "/v1/workspaces/ws/files/a.txt",
            "headers": {"Authorization": "Bearer stolen"},
        },
        {
            "method": "GET",
            "target": "/v1/workspaces/ws/files/a.txt",
            "actor": "admin",
        },
    ]
    for attack in attacks:
        with pytest.raises(SchemaInvalid):
            gateway.bind(CONTEXT, attack)
    assert gateway.dispatch_count == 0
    assert count(clean_postgres, ActionRecord) == 0


@pytest.mark.postgres
def test_at_015_get_is_side_effect_free(clean_postgres: Engine) -> None:
    app = create_app(engine=clean_postgres, authenticator=p02_authenticator())
    client = TestClient(app)
    created = client.put(
        "/v1/workspaces/ws/files/data/input.txt",
        headers={
            **request_headers(key="get-side-effect-seed"),
            "If-None-Match": "*",
            "Content-Type": "text/plain",
        },
        content="source",
    )
    assert created.status_code == 201
    before = (
        count(clean_postgres, ActionRecord),
        count(clean_postgres, ArtifactRecord),
        app.state.action_gateway.dispatch_count,
    )

    first = client.get("/v1/workspaces/ws/files/data/input.txt", headers=request_headers())
    second = client.get("/v1/workspaces/ws/files/data/input.txt", headers=request_headers())
    after = (
        count(clean_postgres, ActionRecord),
        count(clean_postgres, ArtifactRecord),
        app.state.action_gateway.dispatch_count,
    )

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b"source"
    assert first.headers["etag"] == created.headers["etag"]
    assert after == before


@pytest.mark.postgres
def test_at_016_concurrent_if_match_has_one_winner(clean_postgres: Engine) -> None:
    app = create_app(engine=clean_postgres, authenticator=p02_authenticator())
    seed_client = TestClient(app)
    created = seed_client.put(
        "/v1/workspaces/ws/files/race.txt",
        headers={
            **request_headers(key="race-create"),
            "If-None-Match": "*",
            "Content-Type": "text/plain",
        },
        content="v1",
    )
    etag = created.headers["etag"]

    def update_file(index: int) -> tuple[int, str]:
        with TestClient(app) as client:
            response = client.put(
                "/v1/workspaces/ws/files/race.txt",
                headers={
                    **request_headers(key=f"race-{index}"),
                    "If-Match": etag,
                    "Content-Type": "text/plain",
                },
                content=f"winner-{index}",
            )
            return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update_file, (1, 2)))

    assert sorted(status for status, _ in outcomes) == [200, 412]
    current = seed_client.get("/v1/workspaces/ws/files/race.txt", headers=request_headers())
    assert current.content in {b"winner-1", b"winner-2"}
    with Session(clean_postgres) as session:
        entry = session.scalar(select(WorkspaceEntryRecord))
        assert entry is not None
        assert entry.revision == 2
    assert count(clean_postgres, ArtifactRecord) == 2


@pytest.mark.postgres
def test_at_017_create_and_overwrite_preconditions(clean_postgres: Engine) -> None:
    client = TestClient(create_app(engine=clean_postgres, authenticator=p02_authenticator()))
    url = "/v1/workspaces/ws/files/preconditions.txt"
    common = {**request_headers(key="precondition-missing"), "Content-Type": "text/plain"}

    missing = client.put(url, headers=common, content="missing")
    double = client.put(
        url,
        headers={
            **request_headers(key="precondition-double"),
            "Content-Type": "text/plain",
            "If-Match": '"old"',
            "If-None-Match": "*",
        },
        content="double",
    )
    created = client.put(
        url,
        headers={
            **request_headers(key="precondition-create"),
            "Content-Type": "text/plain",
            "If-None-Match": "*",
        },
        content="created",
    )
    create_again = client.put(
        url,
        headers={
            **request_headers(key="precondition-create-again"),
            "Content-Type": "text/plain",
            "If-None-Match": "*",
        },
        content="overwrite",
    )

    assert missing.status_code == 428
    assert missing.json()["code"] == "precondition_required"
    assert double.status_code == 400
    assert created.status_code == 201
    assert create_again.status_code == 412
    fetched = client.get(url, headers=request_headers())
    assert fetched.content == b"created"


@pytest.mark.postgres
def test_at_018_path_traversal_and_host_symlink_inputs_are_rejected(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    bait = tmp_path / "outside.txt"
    bait.write_text("untouched", encoding="utf-8")
    (tmp_path / "link").symlink_to(bait)
    attacks = (
        "../outside.txt",
        "safe/../../outside.txt",
        "%252e%252e/outside.txt",
        "/etc/passwd",
        "safe\\outside.txt",
        "safe/\0outside.txt",
        "safe//outside.txt",
        "link/../../outside.txt",
    )
    for attack in attacks:
        with pytest.raises(InvalidOperation):
            normalize_file_path(attack)
    assert normalize_file_path("reports/2026/result.txt") == "reports/2026/result.txt"

    client = TestClient(create_app(engine=clean_postgres, authenticator=p02_authenticator()))
    logical_link = client.put(
        "/v1/workspaces/ws/files/link",
        headers={
            **request_headers(key="logical-symlink-name"),
            "Content-Type": "text/plain",
            "If-None-Match": "*",
        },
        content="managed database content",
    )
    assert logical_link.status_code == 201
    assert (
        client.get("/v1/workspaces/ws/files/link", headers=request_headers()).content
        == b"managed database content"
    )
    assert bait.read_text(encoding="utf-8") == "untouched"


@pytest.mark.postgres
def test_at_019_http_and_inproc_use_same_binding_and_truthful_transport(
    clean_postgres: Engine,
) -> None:
    app = create_app(engine=clean_postgres, authenticator=p02_authenticator())
    client = TestClient(app)
    http_response = client.post(
        "/v1/integrations/native/operations/echo/invocations",
        headers=request_headers(key="http-echo"),
        json={"message": "same"},
    )
    assert http_response.status_code == 202
    http_action_id = http_response.json()["resource_id"]

    controller = RunController(clean_postgres)
    accepted = controller.admit_run(
        CONTEXT,
        {"agent_id": "agent", "input": "echo", "limits": {"max_tool_calls": 1}},
        "inproc-run",
    )
    inproc = app.state.action_gateway.execute(
        CONTEXT,
        echo_operation("same"),
        idempotency_key="inproc-echo",
        transport_kind="inproc",
        run_id=accepted.resource_id,
        turn_id=1,
        call_index=0,
    )
    http_action = controller.get_action(CONTEXT, http_action_id)

    assert http_action.request_hash == inproc.request_hash
    assert controller.get_run(CONTEXT, http_action.run_id).status == RunStatus.SUCCEEDED
    assert http_action.result is not None
    assert http_action.result["value"] == inproc.value == {"message": "same"}
    with Session(clean_postgres) as session:
        exchanges = session.scalars(
            select(HttpExchangeRecord).order_by(HttpExchangeRecord.transport_kind)
        ).all()
    assert [exchange.transport_kind for exchange in exchanges] == ["http", "inproc"]
    assert all(exchange.request_hash == inproc.request_hash for exchange in exchanges)
    with Session(clean_postgres) as session:
        assert (
            session.scalar(select(JobRecord.status).where(JobRecord.run_id == http_action.run_id))
            == "done"
        )


@pytest.mark.postgres
def test_at_020_execute_bound_does_not_create_a_second_action(clean_postgres: Engine) -> None:
    app = create_app(engine=clean_postgres, authenticator=p02_authenticator())
    controller = RunController(clean_postgres)
    accepted = controller.admit_run(
        CONTEXT,
        {"agent_id": "agent", "input": "once", "limits": {"max_tool_calls": 1}},
        "one-action-run",
    )
    gateway: ActionGateway = app.state.action_gateway

    first = gateway.execute(
        CONTEXT,
        echo_operation("once"),
        idempotency_key="one-action",
        transport_kind="inproc",
        run_id=accepted.resource_id,
        turn_id=7,
        call_index=3,
    )
    second = gateway.execute(
        CONTEXT,
        echo_operation("once"),
        idempotency_key="one-action",
        transport_kind="inproc",
        run_id=accepted.resource_id,
        turn_id=7,
        call_index=3,
    )

    assert first.action_id == second.action_id
    assert second.replayed is True
    assert gateway.dispatch_count == 1
    assert count(clean_postgres, ActionRecord) == 1
    assert count(clean_postgres, HttpExchangeRecord) == 1


@pytest.mark.postgres
def test_at_021_capability_schema_is_a_second_validation_layer(
    clean_postgres: Engine,
) -> None:
    app = create_app(engine=clean_postgres, authenticator=p02_authenticator())
    gateway: ActionGateway = app.state.action_gateway
    missing = echo_operation().model_dump(mode="json")
    missing["payload"]["value"] = {}
    wrong_type = echo_operation(42).model_dump(mode="json")
    deep: dict[str, Any] = {"leaf": True}
    for _ in range(40):
        deep = {"child": deep}
    too_deep = echo_operation().model_dump(mode="json")
    too_deep["payload"]["value"] = {"message": "hello", "nested": deep}

    for operation in (missing, wrong_type, too_deep):
        with pytest.raises(SchemaInvalid):
            gateway.bind(CONTEXT, operation)
    assert gateway.dispatch_count == 0
    assert count(clean_postgres, ActionRecord) == 0


@pytest.mark.postgres
def test_artifact_http_round_trip_is_immutable_and_authorized(clean_postgres: Engine) -> None:
    client = TestClient(create_app(engine=clean_postgres, authenticator=p02_authenticator()))
    created = client.post(
        "/v1/artifacts",
        headers={
            **request_headers(key="artifact-create"),
            "Content-Type": "application/octet-stream",
        },
        content=b"artifact bytes",
    )
    assert created.status_code == 201
    artifact_id = created.json()["id"]
    metadata = client.get(f"/v1/artifacts/{artifact_id}", headers=request_headers())
    content = client.get(f"/v1/artifacts/{artifact_id}/content", headers=request_headers())

    assert metadata.status_code == content.status_code == 200
    assert metadata.json()["sha256"] == created.json()["sha256"]
    assert content.content == b"artifact bytes"
    assert content.headers["etag"] == f'"sha256:{created.json()["sha256"]}"'
