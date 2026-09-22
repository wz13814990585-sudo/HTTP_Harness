from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from hnh.adapters.postgres.models import (
    ActionAttemptRecord,
    ActionRecord,
    BudgetRecord,
    EventRecord,
    IdempotencyRecord,
    JobRecord,
    RunRecord,
)
from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.application.run_controller import RunController
from hnh.domain.errors import AlreadyTerminal
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.transport.http.app import create_app

TOKEN_A = "test-token-a"
TOKEN_B = "test-token-b"
CONTEXT_A = TrustedContext("tenant", "subject-a", frozenset({"runs:read", "runs:write"}))


def authenticator() -> DevelopmentAuthenticator:
    scopes = frozenset({"runs:read", "runs:write"})
    return DevelopmentAuthenticator(
        {
            TOKEN_A: DevelopmentPrincipal("tenant", "subject-a", scopes),
            TOKEN_B: DevelopmentPrincipal("tenant", "subject-b", scopes),
        }
    )


def headers(token: str, key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def run_payload(goal: str = "produce a report") -> dict[str, Any]:
    return {
        "agent_id": "agent-demo",
        "input": goal,
        "limits": {"max_model_turns": 5, "max_cost_microunits": 1000},
    }


def table_count(engine: Engine, model: type[Any]) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.postgres
def test_at_003_anonymous_run_calls_leave_no_business_state(
    clean_postgres: Engine,
) -> None:
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))

    create_response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "anonymous"}, json=run_payload()
    )
    read_response = client.get("/v1/runs/run_missing")

    assert create_response.status_code == 401
    assert read_response.status_code == 401
    assert create_response.headers["www-authenticate"] == "Bearer"
    for model in (RunRecord, EventRecord, JobRecord, IdempotencyRecord, BudgetRecord):
        assert table_count(clean_postgres, model) == 0


@pytest.mark.postgres
def test_at_004_transaction_failure_never_returns_a_phantom_acceptance(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)

    def fail_before_commit() -> None:
        raise RuntimeError("simulated process loss before commit")

    with pytest.raises(RuntimeError, match="before commit"):
        controller.admit_run(
            CONTEXT_A,
            run_payload(),
            "commit-boundary",
            before_commit=fail_before_commit,
        )

    assert table_count(clean_postgres, RunRecord) == 0
    assert table_count(clean_postgres, IdempotencyRecord) == 0

    accepted = controller.admit_run(CONTEXT_A, run_payload(), "commit-boundary")
    assert accepted.replayed is False
    assert table_count(clean_postgres, RunRecord) == 1
    assert table_count(clean_postgres, EventRecord) == 1
    assert table_count(clean_postgres, JobRecord) == 1


@pytest.mark.postgres
def test_at_005_twenty_concurrent_replays_create_one_run(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)

    def admit(_: int) -> str:
        return controller.admit_run(CONTEXT_A, run_payload(), "same-key").resource_id

    with ThreadPoolExecutor(max_workers=20) as executor:
        run_ids = list(executor.map(admit, range(20)))

    assert len(set(run_ids)) == 1
    assert table_count(clean_postgres, RunRecord) == 1
    assert table_count(clean_postgres, EventRecord) == 1
    assert table_count(clean_postgres, JobRecord) == 1
    assert table_count(clean_postgres, BudgetRecord) == 1
    assert table_count(clean_postgres, IdempotencyRecord) == 1


@pytest.mark.postgres
def test_at_006_same_key_with_changed_payload_is_conflict(
    clean_postgres: Engine,
) -> None:
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))
    first = client.post(
        "/v1/runs", headers=headers(TOKEN_A, "changed-body"), json=run_payload("first")
    )
    second = client.post(
        "/v1/runs", headers=headers(TOKEN_A, "changed-body"), json=run_payload("second")
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_conflict"
    with Session(clean_postgres) as session:
        assert session.scalar(select(RunRecord.goal)) == "first"


@pytest.mark.postgres
def test_at_007_replay_rechecks_current_scope_before_history(
    clean_postgres: Engine,
) -> None:
    auth = authenticator()
    client = TestClient(create_app(engine=clean_postgres, authenticator=auth))
    first = client.post("/v1/runs", headers=headers(TOKEN_A, "revoked"), json=run_payload())
    auth.set_scopes(TOKEN_A, frozenset({"runs:read"}))
    replay = client.post("/v1/runs", headers=headers(TOKEN_A, "revoked"), json=run_payload())

    assert first.status_code == 202
    assert replay.status_code == 403
    assert "resource_id" not in replay.json()
    assert table_count(clean_postgres, RunRecord) == 1


@pytest.mark.postgres
def test_at_008_terminal_run_rejects_new_action(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    accepted = controller.admit_run(CONTEXT_A, run_payload(), "terminal")
    controller.transition_run(
        accepted.resource_id,
        0,
        RunStatus.RUNNING,
        event_type="run.started",
        actor_ref="worker",
    )
    controller.transition_run(
        accepted.resource_id,
        1,
        RunStatus.SUCCEEDED,
        event_type="run.succeeded",
        actor_ref="worker",
    )

    with pytest.raises(AlreadyTerminal):
        controller.record_admitted_action(
            CONTEXT_A,
            accepted.resource_id,
            turn_id=1,
            call_index=0,
            request_hash="0" * 64,
            capability_revision="test:1",
            effect_semantics="read_only",
        )
    assert table_count(clean_postgres, ActionRecord) == 0


@pytest.mark.postgres
def test_at_009_snapshot_and_event_rollback_together(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    accepted = controller.admit_run(CONTEXT_A, run_payload(), "atomic-event")

    def fail_after_snapshot() -> None:
        raise RuntimeError("simulated failure between snapshot and event")

    with pytest.raises(RuntimeError, match="between snapshot and event"):
        controller.transition_run(
            accepted.resource_id,
            0,
            RunStatus.RUNNING,
            event_type="run.started",
            actor_ref="worker",
            after_snapshot_before_event=fail_after_snapshot,
        )

    snapshot = controller.get_run(CONTEXT_A, accepted.resource_id)
    assert snapshot.status == RunStatus.QUEUED
    assert snapshot.version == 0
    assert table_count(clean_postgres, EventRecord) == 1


@pytest.mark.postgres
def test_at_010_duplicate_wakeups_admit_one_action_attempt(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    accepted = controller.admit_run(CONTEXT_A, run_payload(), "action-dispatch")
    controller.transition_run(
        accepted.resource_id,
        0,
        RunStatus.RUNNING,
        event_type="run.started",
        actor_ref="worker",
    )
    action_id = controller.record_admitted_action(
        CONTEXT_A,
        accepted.resource_id,
        turn_id=1,
        call_index=0,
        request_hash="1" * 64,
        capability_revision="test:1",
        effect_semantics="read_only",
    )
    with Session(clean_postgres) as session, session.begin():
        session.execute(update(JobRecord).values(status="done"))
        session.add_all(
            [
                JobRecord(
                    id=f"job_duplicate_{index}",
                    tenant_id="tenant",
                    kind="dispatch_action",
                    action_id=action_id,
                    dedupe_key=f"action:{action_id}:wake:{index}",
                    status="ready",
                )
                for index in range(2)
            ]
        )

    def worker(worker_number: int) -> bool:
        claimed = controller.claim_job(f"worker-{worker_number}")
        assert claimed is not None
        assert claimed.action_id == action_id
        return controller.begin_action_dispatch(action_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, range(2)))

    assert sorted(results) == [False, True]
    assert table_count(clean_postgres, ActionAttemptRecord) == 1
    with Session(clean_postgres) as session:
        assert session.scalar(select(ActionRecord.status)) == ActionStatus.RUNNING.value


@pytest.mark.postgres
def test_at_011_foreign_and_missing_runs_have_same_response(
    clean_postgres: Engine,
) -> None:
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))
    created = client.post("/v1/runs", headers=headers(TOKEN_A, "private-run"), json=run_payload())
    run_id = created.json()["resource_id"]

    foreign = client.get(f"/v1/runs/{run_id}", headers=headers(TOKEN_B))
    missing = client.get("/v1/runs/run_missing", headers=headers(TOKEN_B))

    assert created.status_code == 202
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


@pytest.mark.postgres
def test_run_create_get_and_cancellation_round_trip(clean_postgres: Engine) -> None:
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))
    created = client.post("/v1/runs", headers=headers(TOKEN_A, "round-trip"), json=run_payload())
    run_id = created.json()["resource_id"]
    fetched = client.get(f"/v1/runs/{run_id}", headers=headers(TOKEN_A))
    cancelled = client.post(
        f"/v1/runs/{run_id}/cancellations",
        headers=headers(TOKEN_A, "cancel-round-trip"),
        json={"reason": "user request"},
    )
    fetched_after = client.get(f"/v1/runs/{run_id}", headers=headers(TOKEN_A))

    assert created.status_code == 202
    assert created.headers["location"] == f"/v1/runs/{run_id}"
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "queued"
    assert cancelled.status_code == 202
    assert fetched_after.json()["status"] == "cancelling"
    assert fetched_after.json()["version"] == 1
