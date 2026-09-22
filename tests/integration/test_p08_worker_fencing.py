"""PostgreSQL lease fencing for durable model turns; no worker is advertised yet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from hnh.adapters.postgres.models import ActionRecord, JobRecord
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.domain.errors import InvalidOperation, LeaseLost
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus

ACTOR = TrustedContext("tenant-p08-fence", "subject-p08-fence", frozenset({"runs:read"}))


def _expire(clean_postgres: Engine, job_id: str) -> None:
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.id == job_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )


@pytest.mark.postgres
def test_stale_run_worker_cannot_commit_model_response_after_reclaim(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-fence", "input": "model turn lease fence"},
        "p08-fence-one",
    ).resource_id
    controller.ensure_run_running(run_id, actor_ref="test")
    first = controller.claim_job("worker-one", lease_seconds=30, kind="advance_run")
    assert first is not None and first.run_id == run_id
    first_call = controller.begin_model_call(
        ACTOR,
        run_id,
        turn_id=1,
        provider="scripted",
        model_revision="test-1",
        request_payload={"max_output_tokens": 100},
        claimed_job=first,
        worker_id="worker-one",
    )
    assert first_call.attempt_id is not None
    _expire(clean_postgres, first.id)
    second = controller.claim_job("worker-two", lease_seconds=30, kind="advance_run")
    assert second is not None and second.id == first.id
    assert second.lease_epoch > first.lease_epoch
    with pytest.raises(LeaseLost):
        controller.complete_model_call(
            first_call.call.id,
            first_call.attempt_id,
            response_payload={"output": "stale"},
            parsed_output={"public_output": "stale"},
            provider_request_id="stale-provider-response",
            usage={},
            claimed_job=first,
            worker_id="worker-one",
        )
    with pytest.raises(LeaseLost):
        controller.begin_model_call(
            ACTOR,
            run_id,
            turn_id=1,
            provider="scripted",
            model_revision="test-1",
            request_payload={"max_output_tokens": 100},
            claimed_job=first,
            worker_id="worker-one",
        )
    replay = controller.begin_model_call(
        ACTOR,
        run_id,
        turn_id=1,
        provider="scripted",
        model_revision="test-1",
        request_payload={"max_output_tokens": 100},
        claimed_job=second,
        worker_id="worker-two",
    )
    assert replay.attempt_no == 2 and replay.attempt_id is not None
    committed = controller.complete_model_call(
        replay.call.id,
        replay.attempt_id,
        response_payload={"output": "current"},
        parsed_output={"public_output": "current"},
        provider_request_id="current-provider-response",
        usage={"input_tokens": 1, "output_tokens": 1},
        claimed_job=second,
        worker_id="worker-two",
    )
    assert committed.provider_request_id == "current-provider-response"
    with pytest.raises(LeaseLost):
        controller.mark_model_call(
            committed.id,
            "processed",
            claimed_job=first,
            worker_id="worker-one",
        )
    controller.mark_model_call(
        committed.id,
        "processed",
        claimed_job=second,
        worker_id="worker-two",
    )


@pytest.mark.postgres
def test_run_job_fences_action_admission_and_late_result(clean_postgres: Engine) -> None:
    actor = TrustedContext(
        ACTOR.tenant_id,
        ACTOR.subject_id,
        frozenset({"runs:read", "integrations:invoke", "execution:write"}),
    )
    controller = RunController(clean_postgres)
    gateway = ActionGateway(
        clean_postgres, CapabilityRegistry(), ResourceStore(clean_postgres), controller
    )
    run_id = controller.admit_run(
        actor,
        {"agent_id": "p08-fence", "input": "echo once"},
        "p08-fence-action",
    ).resource_id
    controller.ensure_run_running(run_id, actor_ref="test")
    first = controller.claim_job("worker-one", lease_seconds=30, kind="advance_run")
    assert first is not None and first.run_id == run_id
    operation = {
        "method": "POST",
        "target": "/v1/integrations/native/operations/echo/invocations",
        "headers": {"content-type": "application/json"},
        "payload": {"kind": "json", "value": {"message": "same-input"}},
    }
    _expire(clean_postgres, first.id)
    second = controller.claim_job("worker-two", lease_seconds=30, kind="advance_run")
    assert second is not None and second.lease_epoch > first.lease_epoch
    with pytest.raises(LeaseLost):
        gateway.execute(
            actor,
            operation,
            idempotency_key="p08-stale-admission",
            transport_kind="inproc",
            run_id=run_id,
            claimed_job=first,
            worker_id="worker-one",
        )
    with Session(clean_postgres) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ActionRecord).where(ActionRecord.run_id == run_id)
            )
            == 0
        )
    completed = gateway.execute(
        actor,
        operation,
        idempotency_key="p08-current-admission",
        transport_kind="inproc",
        run_id=run_id,
        claimed_job=second,
        worker_id="worker-two",
    )
    assert completed.succeeded and completed.value == {"message": "same-input"}
    with pytest.raises(InvalidOperation, match="lease-bound dispatch"):
        gateway.execute(
            actor,
            {
                "method": "POST",
                "target": "/v1/execution-sessions",
                "headers": {"content-type": "application/json"},
                "payload": {
                    "kind": "json",
                    "value": {
                        "run_id": run_id,
                        "runtime": "python",
                        "profile_id": "standard",
                    },
                },
            },
            idempotency_key="p08-unsafe-worker-denied",
            transport_kind="inproc",
            run_id=run_id,
            claimed_job=second,
            worker_id="worker-two",
        )

    # A second action is started under the current lease, then the response
    # arrives after lease handoff. It cannot be committed by the stale worker.
    bound = gateway.bind(actor, operation)
    admission = controller.get_or_record_admitted_action(
        actor,
        run_id,
        turn_id=2,
        call_index=0,
        request_hash=bound.request_hash,
        capability_id=bound.capability.id,
        capability_revision=bound.capability.revision,
        effect_semantics=bound.capability.effect_semantics,
        bound_operation=bound.parameters,
        claimed_job=second,
        worker_id="worker-two",
    )
    assert controller.begin_action_dispatch(
        admission.action.id, claimed_job=second, worker_id="worker-two"
    )
    _expire(clean_postgres, second.id)
    third = controller.claim_job("worker-three", lease_seconds=30, kind="advance_run")
    assert third is not None and third.lease_epoch > second.lease_epoch
    with pytest.raises(LeaseLost):
        controller.complete_action(
            admission.action.id,
            result={"status_code": 200, "value": {"message": "same-input"}},
            succeeded=True,
            transport_kind="inproc",
            response_status=200,
            claimed_job=second,
            worker_id="worker-two",
        )
    assert controller.get_action(actor, admission.action.id).status == ActionStatus.RUNNING
    assert not controller.begin_action_dispatch(
        admission.action.id, claimed_job=third, worker_id="worker-three"
    )


@pytest.mark.postgres
def test_claimed_run_identity_and_deferred_wakeup_are_lease_bound(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-fence", "input": "wait for actor resolver"},
        "p08-deferred-actor",
    ).resource_id
    claimed = controller.claim_job("worker-one", kind="advance_run")
    assert claimed is not None and claimed.run_id == run_id
    assert controller.claimed_run_actor(claimed, "worker-one") == (
        ACTOR.tenant_id,
        ACTOR.subject_id,
    )
    with pytest.raises(LeaseLost):
        controller.claimed_run_actor(claimed, "worker-wrong")
    controller.defer_job(claimed, "worker-one", delay_seconds=60)
    assert controller.claim_job("worker-two", kind="advance_run") is None
    with pytest.raises(LeaseLost):
        controller.defer_job(claimed, "worker-one", delay_seconds=60)
    with Session(clean_postgres) as session:
        job = session.get(JobRecord, claimed.id)
        assert job is not None and job.status == "ready" and job.lease_owner is None


@pytest.mark.postgres
def test_stale_worker_cannot_write_context_or_budget_after_lease_reclaim(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-fence", "input": "protect context and budget"},
        "p08-fence-context-budget",
    ).resource_id
    first = controller.claim_job("worker-old", kind="advance_run")
    assert first is not None
    reservation = controller.reserve_budget(
        run_id,
        reservation_key="model:1:turn",
        dimension="model_turn",
        amount=1,
        claimed_job=first,
        worker_id="worker-old",
    )
    _expire(clean_postgres, first.id)
    successor = controller.claim_job("worker-new", kind="advance_run")
    assert successor is not None and successor.lease_epoch > first.lease_epoch
    with pytest.raises(LeaseLost):
        controller.reserve_budget(
            run_id,
            reservation_key="model:2:turn",
            dimension="model_turn",
            amount=1,
            claimed_job=first,
            worker_id="worker-old",
        )
    with pytest.raises(LeaseLost):
        controller.settle_budget(
            reservation.id,
            actual_amount=1,
            claimed_job=first,
            worker_id="worker-old",
        )
    with pytest.raises(LeaseLost):
        controller.release_budget(reservation.id, claimed_job=first, worker_id="worker-old")
    builder = ContextBuilder(controller, CapabilityRegistry())
    with pytest.raises(LeaseLost):
        builder.build(ACTOR, run_id, 1, claimed_job=first, worker_id="worker-old")
    assert builder.build(ACTOR, run_id, 1, claimed_job=successor, worker_id="worker-new")
    settled = controller.settle_budget(
        reservation.id,
        actual_amount=1,
        claimed_job=successor,
        worker_id="worker-new",
    )
    assert settled.status == "settled"
