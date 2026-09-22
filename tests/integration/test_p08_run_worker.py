"""A real PostgreSQL job worker exercises fresh actor resolution and lease handoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from time import sleep
from typing import Any

import pytest
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import JobRecord
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.run_worker import ActorResolver, RunWorker
from hnh.application.runner import Runner, RunnerHooks
from hnh.domain.errors import AlreadyTerminal, LeaseLost
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.ports.models import CompleteModelResponse, ContextSnapshot, ModelLimits

ACTOR = TrustedContext(
    "tenant-p08-auto", "subject-p08-auto", frozenset({"runs:read", "integrations:invoke"})
)


def _worker(
    engine: Engine,
    controller: RunController,
    provider: ScriptedProvider,
    resolver: ActorResolver,
    *,
    worker_id: str,
    lease_seconds: int = 30,
) -> RunWorker:
    registry = CapabilityRegistry()
    runner = Runner(
        controller,
        ContextBuilder(controller, registry),
        ActionGateway(engine, registry, ResourceStore(engine), controller),
        CompletionGate(engine, controller),
        provider,
    )
    return RunWorker(controller, runner, resolver, worker_id=worker_id, lease_seconds=lease_seconds)


def _wake_now(engine: Engine, run_id: str) -> None:
    with Session(engine) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.run_id == run_id, JobRecord.kind == "advance_run")
            .values(due_at=datetime.now(UTC) - timedelta(seconds=1))
        )


@pytest.mark.postgres
def test_worker_drains_child_then_parent_cancellation_without_model_call(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    parent_id = controller.admit_run(
        ACTOR,
        {
            "agent_id": "p08-cancel-parent",
            "input": "parent work",
            "limits": {"max_model_turns": 4, "max_tool_calls": 4, "max_output_tokens": 2048},
        },
        "p08-worker-parent-cancel",
    ).resource_id
    child_id = controller.admit_child_run(
        ACTOR,
        parent_id,
        goal="child work",
        requested_scopes=frozenset({"integrations:invoke"}),
        limits={"max_model_turns": 1, "max_tool_calls": 1, "max_output_tokens": 512},
        idempotency_key="p08-worker-child-cancel",
    ).resource_id
    controller.request_cancellation(ACTOR, parent_id, {"reason": "stop"}, "p08-cancel-family")
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.run_id == parent_id, JobRecord.kind == "cancel_run")
            .values(due_at=datetime.now(UTC) - timedelta(seconds=2))
        )
        session.execute(
            update(JobRecord)
            .where(JobRecord.run_id == child_id, JobRecord.kind == "cancel_run")
            .values(due_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    provider = ScriptedProvider([])
    worker = _worker(
        clean_postgres,
        controller,
        provider,
        lambda _tenant, _subject: ACTOR,
        worker_id="worker-cancel-family",
    )
    first = worker.run_once()
    assert first is not None and first.run_id == parent_id
    assert first.disposition == "awaiting_cancellation_evidence"
    assert controller.get_run(ACTOR, parent_id).status == RunStatus.CANCELLING
    second = worker.run_once()
    assert second is not None and second.run_id == child_id
    assert second.disposition == "cancelled"
    assert controller.get_run(ACTOR, child_id).status == RunStatus.CANCELLED
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.run_id == parent_id, JobRecord.kind == "cancel_run")
            .values(due_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    third = worker.run_once()
    assert third is not None and third.run_id == parent_id
    assert third.disposition == "cancelled"
    assert controller.get_run(ACTOR, parent_id).status == RunStatus.CANCELLED
    assert provider.calls == 0


@pytest.mark.postgres
def test_worker_does_not_confirm_cancellation_with_unknown_action(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-unknown-cancel", "input": "external effect may have happened"},
        "p08-worker-unknown-cancel",
    ).resource_id
    controller.ensure_run_running(run_id, actor_ref="test")
    action_id = controller.record_admitted_action(
        ACTOR,
        run_id,
        turn_id=1,
        call_index=0,
        request_hash="a" * 64,
        capability_id="external.publish",
        capability_revision="1",
        effect_semantics="unsafe",
        bound_operation={"target": "publication:p08-unknown"},
    )
    assert controller.begin_action_dispatch(action_id)
    controller.mark_action_outcome_unknown(
        action_id, receipt={"possible_dispatch": True}, upstream_handle=None
    )
    controller.request_cancellation(ACTOR, run_id, {"reason": "stop"}, "p08-unknown-stop")
    provider = ScriptedProvider([])
    worker = _worker(
        clean_postgres,
        controller,
        provider,
        lambda _tenant, _subject: ACTOR,
        worker_id="worker-unknown-cancel",
    )
    step = worker.run_once()
    assert step is not None and step.disposition == "awaiting_cancellation_evidence"
    assert controller.get_run(ACTOR, run_id).status == RunStatus.CANCELLING
    assert controller.get_action(ACTOR, action_id).status == ActionStatus.OUTCOME_UNKNOWN
    assert provider.calls == 0
    with pytest.raises(AlreadyTerminal):
        controller.record_admitted_action(
            ACTOR,
            run_id,
            turn_id=2,
            call_index=0,
            request_hash="b" * 64,
            capability_id="external.publish",
            capability_revision="1",
            effect_semantics="unsafe",
            bound_operation={"target": "publication:too-late"},
        )


@pytest.mark.postgres
def test_stale_cancel_lease_cannot_confirm_run_stop(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-stale-cancel", "input": "stop with current lease only"},
        "p08-stale-cancel-run",
    ).resource_id
    controller.request_cancellation(ACTOR, run_id, {"reason": "stop"}, "p08-stale-cancel")
    stale = controller.claim_job("stale-worker", lease_seconds=3, kind="cancel_run")
    assert stale is not None
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.id == stale.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    current = controller.claim_job("current-worker", lease_seconds=3, kind="cancel_run")
    assert current is not None and current.id == stale.id
    with pytest.raises(LeaseLost):
        controller.finalize_run_cancellation(
            ACTOR, run_id, claimed_job=stale, worker_id="stale-worker"
        )
    assert controller.get_run(ACTOR, run_id).status == RunStatus.CANCELLING
    assert (
        controller.finalize_run_cancellation(
            ACTOR, run_id, claimed_job=current, worker_id="current-worker"
        ).status
        == RunStatus.CANCELLED
    )
    controller.complete_job(current, "current-worker")


@pytest.mark.postgres
def test_late_model_response_after_confirmed_cancel_cannot_admit_action(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-late-model", "input": "echo only if still running"},
        "p08-late-model-run",
    ).resource_id
    provider = ScriptedProvider(
        [
            {
                "operations": [
                    {
                        "method": "POST",
                        "target": "/v1/integrations/native/operations/echo/invocations",
                        "headers": {"content-type": "application/json"},
                        "payload": {"kind": "json", "value": {"message": "too-late"}},
                    }
                ]
            }
        ]
    )
    runner = _worker(
        clean_postgres,
        controller,
        provider,
        lambda _tenant, _subject: ACTOR,
        worker_id="old-model-worker",
    ).runner

    def cancel_before_model_response_commits() -> None:
        controller.request_cancellation(ACTOR, run_id, {"reason": "stop"}, "p08-stop-late-model")
        cancel_worker = _worker(
            clean_postgres,
            controller,
            ScriptedProvider([]),
            lambda _tenant, _subject: ACTOR,
            worker_id="cancel-late-model",
        )
        step = cancel_worker.run_once()
        assert step is not None and step.disposition == "cancelled"

    result = runner.advance(
        ACTOR,
        run_id,
        hooks=RunnerHooks(after_provider_before_persist=cancel_before_model_response_commits),
    )
    assert result.status == RunStatus.CANCELLED
    assert controller.get_run(ACTOR, run_id).status == RunStatus.CANCELLED
    assert controller.list_actions(ACTOR, run_id) == ()
    call = controller.latest_model_call(ACTOR, run_id)
    assert call is not None and call.status == "response_committed"
    assert provider.calls == 1


@pytest.mark.postgres
def test_worker_resolves_actor_per_claim_and_completes_native_read_only_run(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {
            "agent_id": "p08-auto",
            "input": "echo then finish",
            "limits": {"max_model_turns": 2, "max_tool_calls": 1, "max_output_tokens": 2048},
        },
        "p08-auto-worker",
    ).resource_id
    first_turn = {
        "operations": [
            {
                "method": "POST",
                "target": "/v1/integrations/native/operations/echo/invocations",
                "headers": {"content-type": "application/json"},
                "payload": {"kind": "json", "value": {"message": "same-input"}},
            }
        ]
    }
    resolved: list[tuple[str, str]] = []

    def resolve(tenant_id: str, subject_id: str) -> TrustedContext:
        resolved.append((tenant_id, subject_id))
        return ACTOR

    first = _worker(
        clean_postgres, controller, ScriptedProvider([first_turn]), resolve, worker_id="worker-1"
    )
    assert first.run_once() is not None
    assert first.run_once() is None  # durable due_at prevents a hot loop
    actions = controller.list_actions(ACTOR, run_id)
    assert len(actions) == 1 and actions[0].status.value == "succeeded"
    _wake_now(clean_postgres, run_id)
    final_turn = {
        "final_candidate": {
            "answer": "Echo observed.",
            "artifact_ids": [],
            "evidence_ids": [actions[0].id],
            "acceptance_claims": ["echo returned same-input"],
        }
    }
    second = _worker(
        clean_postgres, controller, ScriptedProvider([final_turn]), resolve, worker_id="worker-2"
    )
    assert second.run_once() is not None
    assert controller.get_run(ACTOR, run_id).status == RunStatus.SUCCEEDED
    assert second.run_once() is None
    assert resolved == [(ACTOR.tenant_id, ACTOR.subject_id)] * 2


@pytest.mark.postgres
def test_worker_rejects_resolver_identity_mismatch_without_calling_model(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-auto", "input": "must wait for trusted actor"},
        "p08-auto-resolver",
    ).resource_id
    provider = ScriptedProvider([])
    wrong_actor = TrustedContext("other-tenant", ACTOR.subject_id, ACTOR.scopes)
    worker = _worker(
        clean_postgres,
        controller,
        provider,
        lambda _tenant, _subject: wrong_actor,
        worker_id="worker-wrong",
    )
    step = worker.run_once()
    assert step is not None and step.disposition == "identity_unavailable"
    assert controller.get_run(ACTOR, run_id).status == RunStatus.QUEUED
    assert provider.calls == 0
    assert worker.run_once() is None


@pytest.mark.postgres
def test_worker_defers_transient_model_failure_and_reuses_same_turn(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-auto", "input": "retry model safely"},
        "p08-auto-transient",
    ).resource_id
    failed = _worker(
        clean_postgres,
        controller,
        ScriptedProvider([]),
        lambda _t, _s: ACTOR,
        worker_id="worker-fail",
    )
    step = failed.run_once()
    assert step is not None and step.disposition == "provider_unavailable"
    assert controller.get_run(ACTOR, run_id).status == RunStatus.RUNNING
    call = controller.latest_model_call(ACTOR, run_id)
    assert call is not None and call.status == "started" and call.turn_id == 1
    _wake_now(clean_postgres, run_id)
    evidence_id = controller.get_recent_events(ACTOR, run_id)[0].event_id
    final = _worker(
        clean_postgres,
        controller,
        ScriptedProvider(
            [
                {
                    "final_candidate": {
                        "answer": "Recovered.",
                        "evidence_ids": [evidence_id],
                        "acceptance_claims": ["accepted event exists"],
                    }
                }
            ]
        ),
        lambda _t, _s: ACTOR,
        worker_id="worker-resume",
    )
    assert final.run_once() is not None
    assert controller.get_run(ACTOR, run_id).status == RunStatus.SUCCEEDED


@pytest.mark.postgres
def test_worker_renews_lease_while_provider_is_blocked(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-auto", "input": "long model call"},
        "p08-auto-heartbeat",
    ).resource_id
    entered = Event()
    release = Event()

    class WaitingProvider(ScriptedProvider):
        def generate(
            self,
            context: ContextSnapshot,
            tools: list[dict[str, Any]],
            limits: ModelLimits,
        ) -> CompleteModelResponse:
            entered.set()
            if not release.wait(10):
                raise AssertionError("provider was not released")
            return super().generate(context, tools, limits)

    evidence_id = controller.get_recent_events(ACTOR, run_id)[0].event_id
    provider = WaitingProvider(
        [
            {
                "final_candidate": {
                    "answer": "Done.",
                    "evidence_ids": [evidence_id],
                    "acceptance_claims": ["accepted event exists"],
                }
            }
        ]
    )
    worker = _worker(
        clean_postgres,
        controller,
        provider,
        lambda _t, _s: ACTOR,
        worker_id="worker-heartbeat",
        lease_seconds=3,
    )
    failures: list[Exception] = []

    def run_worker() -> None:
        try:
            worker.run_once()
        except Exception as exc:
            failures.append(exc)

    thread = Thread(target=run_worker)
    thread.start()
    try:
        assert entered.wait(5)
        sleep(3.4)  # exceeds the original lease; DB heartbeat must have renewed it
        assert controller.claim_job("rival", lease_seconds=3, kind="advance_run") is None
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    assert controller.get_run(ACTOR, run_id).status == RunStatus.SUCCEEDED


@pytest.mark.postgres
def test_worker_blocks_on_context_scope_change_before_model_call(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-auto", "input": "scope must stay pinned within a turn"},
        "p08-auto-scope-revocation",
    ).resource_id
    ContextBuilder(controller, CapabilityRegistry()).build(ACTOR, run_id, 1)
    reduced = TrustedContext(ACTOR.tenant_id, ACTOR.subject_id, frozenset({"runs:read"}))
    provider = ScriptedProvider([])
    worker = _worker(
        clean_postgres,
        controller,
        provider,
        lambda _t, _s: reduced,
        worker_id="worker-revoked",
    )
    step = worker.run_once()
    assert step is not None and step.disposition == "blocked"
    assert controller.get_run(ACTOR, run_id).status == RunStatus.BLOCKED
    assert provider.calls == 0
    assert worker.run_once() is None


@pytest.mark.postgres
def test_worker_blocks_unsupported_write_before_action_admission(clean_postgres: Engine) -> None:
    writer = TrustedContext(
        ACTOR.tenant_id, ACTOR.subject_id, ACTOR.scopes | frozenset({"execution:write"})
    )
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        writer,
        {"agent_id": "p08-auto", "input": "write must not bypass lease protection"},
        "p08-auto-unsafe-dispatch",
    ).resource_id
    provider = ScriptedProvider(
        [
            {
                "operations": [
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
                    }
                ]
            }
        ]
    )
    worker = _worker(
        clean_postgres,
        controller,
        provider,
        lambda _t, _s: writer,
        worker_id="worker-no-write",
    )
    step = worker.run_once()
    assert step is not None and step.disposition == "blocked"
    run = controller.get_run(writer, run_id)
    assert run.status == RunStatus.BLOCKED
    assert run.failure is not None and run.failure["code"] == "lease_dispatch_unsupported"
    assert controller.list_actions(writer, run_id) == ()
    assert provider.calls == 1
