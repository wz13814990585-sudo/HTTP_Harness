from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import (
    ActionAttemptRecord,
    ActionRecord,
    CheckpointRecord,
    ContextSnapshotRecord,
    EventRecord,
    HttpExchangeRecord,
    InputRequestRecord,
    InputResponseRecord,
    JobRecord,
    ReconciliationRecord,
)
from hnh.application.action_gateway import ActionGateway
from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.application.capabilities import CapabilityRegistry, builtin_capabilities
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.egress import EgressPolicy
from hnh.application.operations import HttpOperation
from hnh.application.recovery import RecoveryCoordinator
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.runner import Runner
from hnh.application.security import redact
from hnh.domain.errors import (
    AlreadyTerminal,
    ApprovalRequired,
    InputRequestExpired,
    InvalidOperation,
    LeaseLost,
    MigrationRequired,
    OutcomeUnknown,
    PermissionDenied,
    VersionConflict,
)
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.ports.effects import (
    AmbiguousDispatch,
    EffectCancellation,
    EffectDispatchResult,
    EffectReconciliation,
)
from hnh.transport.http.app import create_app

TOKEN = "p05-token"
LOW_TOKEN = "p05-low-token"
SCOPES = frozenset(
    {
        "actions:read",
        "actions:reconcile",
        "artifacts:read",
        "artifacts:write",
        "capabilities:read",
        "execution:read",
        "execution:write",
        "inputs:read",
        "inputs:respond",
        "integrations:invoke",
        "runs:read",
        "runs:write",
        "workspace:read",
        "workspace:write",
    }
)
CONTEXT = TrustedContext("tenant-p05", "subject-p05", SCOPES)


class FaultEffectServer:
    def __init__(
        self,
        mode: str = "success",
        *,
        query_supported: bool = True,
        cancel_stopped: bool = False,
    ) -> None:
        self.mode = mode
        self.query_supported = query_supported
        self.cancel_stopped = cancel_stopped
        self.effect_count = 0
        self.dispatch_count = 0
        self.keys: list[str | None] = []
        self.handles: dict[str, bool] = {}
        self.dedupe: dict[str, str] = {}
        self.remote_running = True

    def dispatch(
        self,
        action_id: str,
        _operation: dict[str, Any],
        downstream_idempotency_key: str | None,
    ) -> EffectDispatchResult:
        self.dispatch_count += 1
        self.keys.append(downstream_idempotency_key)
        if downstream_idempotency_key and downstream_idempotency_key in self.dedupe:
            handle = self.dedupe[downstream_idempotency_key]
            return EffectDispatchResult(
                200,
                {"deduplicated": True},
                applied=True,
                upstream_handle=handle,
            )
        self.effect_count += 1
        handle = f"effect-{self.effect_count}"
        self.handles[handle] = True
        if downstream_idempotency_key:
            self.dedupe[downstream_idempotency_key] = handle
        if self.mode in {"lost_after_apply", "idempotent_lost_once"}:
            if self.mode == "lost_after_apply" or self.dispatch_count == 1:
                raise AmbiguousDispatch(
                    {"phase": "response_lost", "action_id": action_id},
                    upstream_handle=handle,
                )
        if self.mode == "unsafe_500_applied":
            return EffectDispatchResult(
                500,
                {"error": "after commit"},
                applied=True,
                upstream_handle=handle,
            )
        return EffectDispatchResult(200, {"ok": True}, applied=True, upstream_handle=handle)

    def reconcile(
        self,
        _action_id: str,
        upstream_handle: str | None,
        downstream_idempotency_key: str | None,
    ) -> EffectReconciliation | None:
        if not self.query_supported:
            return None
        handle = upstream_handle
        if handle is None and downstream_idempotency_key is not None:
            handle = self.dedupe.get(downstream_idempotency_key)
        if handle is not None and self.handles.get(handle):
            return EffectReconciliation(
                "confirmed_applied",
                (f"receipt:{handle}",),
                "effect server query confirmed one application",
            )
        return EffectReconciliation(
            "confirmed_not_applied",
            ("receipt:not-applied",),
        )

    def cancel(
        self,
        _action_id: str,
        _upstream_handle: str | None,
    ) -> EffectCancellation:
        if self.cancel_stopped:
            self.remote_running = False
        return EffectCancellation(
            acknowledged=True,
            stopped=self.cancel_stopped,
            receipt={"remote_state": "stopped" if self.cancel_stopped else "running"},
        )


def authenticator() -> DevelopmentAuthenticator:
    return DevelopmentAuthenticator(
        {
            TOKEN: DevelopmentPrincipal(CONTEXT.tenant_id, CONTEXT.subject_id, SCOPES),
            LOW_TOKEN: DevelopmentPrincipal(
                CONTEXT.tenant_id,
                CONTEXT.subject_id,
                frozenset({"actions:read", "runs:read"}),
            ),
        }
    )


def headers(token: str = TOKEN, key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def running_run(controller: RunController, label: str) -> str:
    run_id = controller.admit_run(
        CONTEXT,
        {
            "agent_id": "agent-p05",
            "input": label,
            "limits": {"max_tool_calls": 20, "max_model_turns": 10},
        },
        f"run-{label}",
    ).resource_id
    controller.ensure_run_running(run_id, actor_ref="test")
    return run_id


def admitted_action(
    controller: RunController,
    run_id: str,
    label: str,
    *,
    effect_semantics: str = "unsafe",
    call_index: int = 0,
) -> str:
    return controller.record_admitted_action(
        CONTEXT,
        run_id,
        turn_id=1,
        call_index=call_index,
        request_hash=(label.encode().hex() + "0" * 64)[:64],
        capability_id="external.publish",
        capability_revision="1",
        effect_semantics=effect_semantics,
        bound_operation={"target": f"publication:{label}", "body_sha256": label},
    )


def table_count(engine: Engine, model: type[Any]) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.postgres
def test_at_039_approval_is_bound_to_exact_action_and_resource_version(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-039")
    action_a = admitted_action(controller, run_id, "request-a")
    request_a = controller.request_approval(
        CONTEXT,
        action_a,
        prompt="Publish document A",
        target_summary="publish document version v1",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        resource_versions={"document": "v1"},
    )
    controller.respond_to_input(
        CONTEXT,
        request_a.id,
        expected_version=0,
        decision="approve",
        values=None,
        comment="approve exact v1",
        idempotency_key="approve-a",
    )
    controller.validate_approval(
        CONTEXT,
        action_a,
        resource_versions={"document": "v1"},
    )

    controller.ensure_run_running(run_id, actor_ref="test")
    action_b = admitted_action(controller, run_id, "request-b", call_index=1)
    with pytest.raises(ApprovalRequired):
        controller.validate_approval(
            CONTEXT,
            action_b,
            input_request_id=request_a.id,
            resource_versions={"document": "v2"},
        )
    request_b = controller.request_approval(
        CONTEXT,
        action_b,
        prompt="Publish document B",
        target_summary="publish document version v2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        resource_versions={"document": "v2"},
    )

    assert request_b.id != request_a.id
    assert request_b.request_hash != request_a.request_hash
    assert request_b.resource_versions == {"document": "v2"}
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(InputRequestRecord)) == 2


@pytest.mark.postgres
def test_at_040_approval_is_consumed_once_and_expiry_blocks_execution(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-040")
    action_id = admitted_action(controller, run_id, "consume-once")
    request = controller.request_approval(
        CONTEXT,
        action_id,
        prompt="Approve once",
        target_summary="one publication",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    def answer(index: int) -> str:
        try:
            controller.respond_to_input(
                CONTEXT,
                request.id,
                expected_version=0,
                decision="approve",
                values=None,
                comment="one answer",
                idempotency_key=f"approval-race-{index}",
            )
            return "accepted"
        except VersionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(answer, range(2)))
    assert sorted(results) == ["accepted", "conflict"]
    assert table_count(clean_postgres, InputResponseRecord) == 1

    controller.ensure_run_running(run_id, actor_ref="test")
    server = FaultEffectServer()
    result = RecoveryCoordinator(controller).dispatch(CONTEXT, action_id, server)
    assert result.action.status == ActionStatus.SUCCEEDED
    assert server.effect_count == 1

    expiry_run = running_run(controller, "at-040-expired")
    expiry_action = admitted_action(controller, expiry_run, "expired")
    expiring = controller.request_approval(
        CONTEXT,
        expiry_action,
        prompt="Will expire",
        target_summary="expired publication",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(InputRequestRecord)
            .where(InputRequestRecord.id == expiring.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(InputRequestExpired):
        controller.respond_to_input(
            CONTEXT,
            expiring.id,
            expected_version=0,
            decision="approve",
            values=None,
            comment=None,
            idempotency_key="expired-response",
        )
    assert controller.get_input_request(CONTEXT, expiring.id).status == "expired"
    assert controller.get_action(CONTEXT, expiry_action).status == ActionStatus.WAITING_INPUT


@pytest.mark.postgres
def test_at_041_approval_wait_survives_controller_restart(clean_postgres: Engine) -> None:
    before_restart = RunController(clean_postgres)
    run_id = running_run(before_restart, "at-041")
    action_id = admitted_action(before_restart, run_id, "restart")
    request = before_restart.request_approval(
        CONTEXT,
        action_id,
        prompt="Persist across restart",
        target_summary="restart-safe publication",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    after_restart = RunController(clean_postgres)
    reloaded = after_restart.get_input_request(CONTEXT, request.id)
    assert reloaded.status == "pending"
    assert reloaded.request_hash == request.request_hash
    assert after_restart.get_run(CONTEXT, run_id).status == RunStatus.WAITING_INPUT
    after_restart.respond_to_input(
        CONTEXT,
        request.id,
        expected_version=reloaded.version,
        decision="approve",
        values=None,
        comment="continue",
        idempotency_key="restart-approval",
    )
    after_restart.ensure_run_running(run_id, actor_ref="restarted-worker")
    server = FaultEffectServer()
    RecoveryCoordinator(after_restart).dispatch(CONTEXT, action_id, server)

    assert server.effect_count == 1
    assert after_restart.get_action(CONTEXT, action_id).status == ActionStatus.SUCCEEDED


@pytest.mark.postgres
def test_at_042_dispatch_rechecks_current_scope_after_approval(
    clean_postgres: Engine,
) -> None:
    native = next(item for item in builtin_capabilities() if item.id == "native.echo")
    registry = CapabilityRegistry((replace(native, requires_approval=True),))
    controller = RunController(clean_postgres)
    gateway = ActionGateway(
        clean_postgres,
        registry,
        ResourceStore(clean_postgres),
        controller,
    )
    operation = HttpOperation(
        method="POST",
        target="/v1/integrations/native/operations/echo/invocations",
        headers={"content-type": "application/json"},
        payload={"kind": "json", "value": {"message": "approved"}},
    )
    proposed = gateway.execute(
        CONTEXT,
        operation,
        idempotency_key="at-042",
        transport_kind="inproc",
    )
    request_id = str(proposed.value["input_request_id"])
    controller.respond_to_input(
        CONTEXT,
        request_id,
        expected_version=0,
        decision="approve",
        values=None,
        comment=None,
        idempotency_key="at-042-approve",
    )
    revoked = TrustedContext(
        CONTEXT.tenant_id,
        CONTEXT.subject_id,
        frozenset(scope for scope in SCOPES if scope != "integrations:invoke"),
    )

    with pytest.raises(PermissionDenied):
        gateway.execute(
            revoked,
            operation,
            idempotency_key="at-042",
            transport_kind="inproc",
        )
    assert gateway.dispatch_count == 0
    assert controller.get_action(CONTEXT, proposed.action_id).status == ActionStatus.READY
    assert table_count(clean_postgres, HttpExchangeRecord) == 0


@pytest.mark.postgres
def test_at_043_lost_response_reconciles_without_repeating_unsafe_effect(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-043")
    action_id = admitted_action(controller, run_id, "lost-response")
    server = FaultEffectServer("lost_after_apply")
    recovery = RecoveryCoordinator(controller)

    first = recovery.dispatch(CONTEXT, action_id, server)
    assert first.action.status == ActionStatus.OUTCOME_UNKNOWN
    assert first.decision == "reconcile_only"
    assert server.effect_count == server.dispatch_count == 1
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.BLOCKED

    reconciled = recovery.reconcile(
        CONTEXT,
        action_id,
        server,
        idempotency_key="at-043-reconcile",
    )
    assert reconciled.action.status == ActionStatus.SUCCEEDED
    assert server.effect_count == server.dispatch_count == 1
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.QUEUED
    assert table_count(clean_postgres, ReconciliationRecord) == 1


@pytest.mark.postgres
def test_at_044_remote_idempotent_retry_reuses_key_and_one_effect(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-044")
    action_id = admitted_action(
        controller,
        run_id,
        "idempotent",
        effect_semantics="remote_idempotent",
    )
    server = FaultEffectServer("idempotent_lost_once")
    recovery = RecoveryCoordinator(controller)

    first = recovery.dispatch(CONTEXT, action_id, server)
    second = recovery.dispatch(CONTEXT, action_id, server)

    assert first.action.status == ActionStatus.RETRY_WAIT
    assert second.action.status == ActionStatus.SUCCEEDED
    assert server.dispatch_count == 2
    assert server.effect_count == 1
    assert server.keys[0] == server.keys[1] == f"hnh:{action_id}"
    with Session(clean_postgres) as session:
        attempts = session.scalars(
            select(ActionAttemptRecord)
            .where(ActionAttemptRecord.action_id == action_id)
            .order_by(ActionAttemptRecord.attempt_no)
        ).all()
        assert [item.status for item in attempts] == ["outcome_unknown", "succeeded"]
        assert len({item.downstream_idempotency_key for item in attempts}) == 1


@pytest.mark.postgres
def test_at_045_unknown_without_query_stays_blocked_and_operator_is_restricted(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-045")
    action_id = admitted_action(controller, run_id, "no-query")
    server = FaultEffectServer("lost_after_apply", query_supported=False)
    recovery = RecoveryCoordinator(controller)
    recovery.dispatch(CONTEXT, action_id, server)

    with pytest.raises(OutcomeUnknown):
        recovery.reconcile(
            CONTEXT,
            action_id,
            server,
            idempotency_key="automatic-reconcile",
        )
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))
    denied = client.post(
        f"/v1/actions/{action_id}/reconciliations",
        headers={**headers(LOW_TOKEN, "manual-denied"), "If-Match": '"2"'},
        json={
            "action_id": action_id,
            "resolution": "confirmed_applied",
            "evidence_ids": ["operator-ticket-1"],
        },
    )

    assert denied.status_code == 403
    assert server.effect_count == server.dispatch_count == 1
    assert controller.get_action(CONTEXT, action_id).status == ActionStatus.OUTCOME_UNKNOWN
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.BLOCKED
    assert table_count(clean_postgres, ActionAttemptRecord) == 1
    assert table_count(clean_postgres, ReconciliationRecord) == 0


@pytest.mark.postgres
def test_at_046_stale_worker_epoch_cannot_dispatch_or_commit(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-046")
    action_id = admitted_action(controller, run_id, "fencing")
    with Session(clean_postgres) as session, session.begin():
        session.execute(update(JobRecord).values(status="done"))
        session.add(
            JobRecord(
                id="job_at_046",
                tenant_id=CONTEXT.tenant_id,
                kind="dispatch_action",
                run_id=run_id,
                action_id=action_id,
                dedupe_key="at-046-dispatch",
                status="ready",
                due_at=datetime.now(UTC),
            )
        )
    worker_one = controller.claim_job("worker-one", lease_seconds=30)
    assert worker_one is not None
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.id == worker_one.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    worker_two = controller.claim_job("worker-two", lease_seconds=30)
    assert worker_two is not None and worker_two.lease_epoch > worker_one.lease_epoch
    server = FaultEffectServer()

    with pytest.raises(LeaseLost):
        controller.begin_action_dispatch(
            action_id,
            claimed_job=worker_one,
            worker_id="worker-one",
        )
    assert server.effect_count == 0
    assert controller.begin_action_dispatch(
        action_id,
        claimed_job=worker_two,
        worker_id="worker-two",
    )
    effect = server.dispatch(action_id, {}, None)
    assert server.effect_count == 1
    with pytest.raises(LeaseLost):
        controller.complete_action(
            action_id,
            result=effect.body,
            succeeded=True,
            transport_kind="effect_driver",
            response_status=200,
            claimed_job=worker_one,
            worker_id="worker-one",
        )
    controller.complete_action(
        action_id,
        result=effect.body,
        succeeded=True,
        transport_kind="effect_driver",
        response_status=200,
        claimed_job=worker_two,
        worker_id="worker-two",
    )
    assert controller.get_action(CONTEXT, action_id).status == ActionStatus.SUCCEEDED


@pytest.mark.postgres
def test_at_047_cancel_and_completion_race_has_one_version_winner(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    outcomes: dict[str, int] = {"succeeded": 0, "cancelling": 0}
    for index in range(12):
        run_id = running_run(controller, f"at-047-{index}")

        def complete(target_run_id: str = run_id) -> str:
            try:
                controller.transition_run(
                    target_run_id,
                    1,
                    RunStatus.SUCCEEDED,
                    event_type="run.succeeded",
                    actor_ref="worker",
                )
                return "completed"
            except VersionConflict as exc:
                return type(exc).__name__

        def cancel(target_run_id: str = run_id, attempt: int = index) -> str:
            try:
                controller.request_cancellation(
                    CONTEXT,
                    target_run_id,
                    {"reason": "race"},
                    f"cancel-race-{attempt}",
                )
                return "cancelled"
            except AlreadyTerminal as exc:
                return type(exc).__name__

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(complete), executor.submit(cancel)]
            _ = [future.result() for future in results]
        final = controller.get_run(CONTEXT, run_id)
        assert final.status in {RunStatus.SUCCEEDED, RunStatus.CANCELLING}
        outcomes[final.status.value] += 1
        if final.status == RunStatus.SUCCEEDED:
            with pytest.raises(AlreadyTerminal):
                controller.request_cancellation(
                    CONTEXT,
                    run_id,
                    {"reason": "late"},
                    f"late-{index}",
                )

    dispatch_run = running_run(controller, "at-047-dispatch")
    action_id = admitted_action(controller, dispatch_run, "must-not-dispatch")
    controller.request_cancellation(
        CONTEXT,
        dispatch_run,
        {"reason": "stop"},
        "at-047-stop",
    )
    assert controller.begin_action_dispatch(action_id) is False
    assert outcomes["succeeded"] + outcomes["cancelling"] == 12


@pytest.mark.postgres
def test_at_048_cancel_ack_is_not_confirmation_of_stop(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-048")
    action_id = admitted_action(controller, run_id, "slow-remote")
    assert controller.begin_action_dispatch(action_id)
    controller.request_cancellation(
        CONTEXT,
        run_id,
        {"reason": "user requested stop"},
        "at-048-cancel",
    )
    server = FaultEffectServer(cancel_stopped=False)
    result = RecoveryCoordinator(controller).cancel(CONTEXT, action_id, server)

    assert result.decision == "awaiting_confirmation"
    assert result.action.status == ActionStatus.WAITING_EXTERNAL
    assert server.remote_running is True
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.CANCELLING


@pytest.mark.postgres
def test_at_049_unsafe_500_with_effect_is_not_retried(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-049")
    action_id = admitted_action(controller, run_id, "unsafe-500")
    server = FaultEffectServer("unsafe_500_applied")
    recovery = RecoveryCoordinator(controller)

    result = recovery.dispatch(CONTEXT, action_id, server)
    assert result.action.status == ActionStatus.OUTCOME_UNKNOWN
    assert result.decision == "reconcile_only"
    assert server.effect_count == server.dispatch_count == 1
    with pytest.raises(VersionConflict):
        recovery.dispatch(CONTEXT, action_id, server)
    assert server.effect_count == server.dispatch_count == 1


def test_at_050_dns_redirect_and_cross_origin_secret_policy() -> None:
    answers = {
        "public.test": ["93.184.216.34"],
        "other.test": ["93.184.216.35"],
    }
    policy = EgressPolicy(
        frozenset(answers),
        resolver=lambda host: answers[host],
    )
    first = policy.validate("https://public.test/resource")
    redirected, forwarded = policy.redirect(
        first,
        "https://other.test/next",
        {
            "Authorization": "Bearer secret-canary",
            "Cookie": "session=secret-canary",
            "X-Request-ID": "safe",
        },
        redirect_count=0,
    )
    assert redirected.origin[1] == "other.test"
    assert forwarded == {"X-Request-ID": "safe"}

    answers["public.test"] = ["127.0.0.1"]
    with pytest.raises(InvalidOperation):
        policy.validate("https://public.test/rebound")
    with pytest.raises(InvalidOperation):
        policy.validate("https://169.254.169.254/latest/meta-data")
    with pytest.raises(InvalidOperation):
        policy.redirect(
            first,
            "https://169.254.169.254/latest/meta-data",
            {"Authorization": "Bearer secret-canary"},
            redirect_count=0,
        )


@pytest.mark.postgres
def test_at_051_events_context_and_exports_redact_secret_canaries(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-051")
    action_id = admitted_action(controller, run_id, "secret-result")
    assert controller.begin_action_dispatch(action_id)
    canary = "sk-SECRETABC123"
    controller.complete_action(
        action_id,
        result={
            "authorization": f"Bearer {canary}",
            "Cookie": f"session={canary}",
            "opaque_state": canary,
            "message": f"provider accidentally returned {canary}",
        },
        succeeded=True,
        transport_kind="effect_driver",
        response_status=200,
    )
    second = admitted_action(controller, run_id, "secret-cancel", call_index=1)
    assert controller.begin_action_dispatch(second)
    controller.request_cancellation(
        CONTEXT,
        run_id,
        {"reason": f"Bearer {canary}"},
        "at-051-cancel",
    )
    controller.record_action_cancel_receipt(
        second,
        receipt={"cookie": canary, "message": f"Bearer {canary}"},
        stopped=False,
    )
    ContextBuilder(controller, CapabilityRegistry()).build(CONTEXT, run_id, 1)

    with Session(clean_postgres) as session:
        events = [row.data for row in session.scalars(select(EventRecord)).all()]
        contexts = [row.content for row in session.scalars(select(ContextSnapshotRecord)).all()]
        exchanges = [
            {
                "request_hash": row.request_hash,
                "response_status": row.response_status,
                "transport_kind": row.transport_kind,
            }
            for row in session.scalars(select(HttpExchangeRecord)).all()
        ]
        raw_result = session.get(ActionRecord, action_id)
        assert raw_result is not None and raw_result.result is not None
        safe_export = redact(raw_result.result)
    serialized = json.dumps(
        {"events": events, "contexts": contexts, "exchanges": exchanges, "export": safe_export}
    )
    assert canary not in serialized
    assert "secret-canary" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.postgres
def test_at_052_incompatible_checkpoint_blocks_recovery(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "at-052")
    checkpoint = controller.save_checkpoint(
        CONTEXT,
        run_id,
        checkpoint_seq=1,
        schema_version="1",
        capability_id="removed.publish",
        capability_revision="old-7",
        runner_cursor={"turn_id": 4},
        content={"pending_action": "publish"},
    )

    with pytest.raises(MigrationRequired):
        controller.recover_latest_checkpoint(
            CONTEXT,
            run_id,
            supported_schema_version="1",
            capability_revisions={"replacement.publish": "new-1"},
            policy_revision="dev-1",
        )
    run = controller.get_run(CONTEXT, run_id)
    assert run.status == RunStatus.BLOCKED
    assert run.failure is not None and run.failure["code"] == "migration_required"
    with Session(clean_postgres) as session:
        stored = session.get(CheckpointRecord, checkpoint.id)
        event = session.scalar(
            select(EventRecord).where(
                EventRecord.run_id == run_id,
                EventRecord.event_type == "run.migration_required",
            )
        )
        assert stored is not None and stored.capability_revision == "old-7"
        assert event is not None and event.data["capability_revision"] == "old-7"


@pytest.mark.postgres
def test_p05_trusted_http_input_and_reconciliation_routes(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "p05-http")
    approval_action = admitted_action(controller, run_id, "http-approval")
    request = controller.request_approval(
        CONTEXT,
        approval_action,
        prompt="Approve through HTTP",
        target_summary="HTTP approval target",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client = TestClient(create_app(engine=clean_postgres, authenticator=authenticator()))

    fetched = client.get(f"/v1/input-requests/{request.id}", headers=headers())
    assert fetched.status_code == 200
    assert fetched.headers["etag"] == '"0"'
    approved = client.post(
        f"/v1/input-requests/{request.id}/responses",
        headers={**headers(key="http-approval-response"), "If-Match": fetched.headers["etag"]},
        json={"decision": "approve", "comment": "trusted channel"},
    )
    assert approved.status_code == 202
    assert approved.headers["location"] == f"/v1/input-requests/{request.id}"

    controller.ensure_run_running(run_id, actor_ref="test")
    unknown_action = admitted_action(controller, run_id, "http-reconciliation", call_index=1)
    assert controller.begin_action_dispatch(unknown_action)
    unknown = controller.mark_action_outcome_unknown(
        unknown_action,
        receipt={"possible_dispatch": True},
        upstream_handle="remote-http-1",
    )
    reconciled = client.post(
        f"/v1/actions/{unknown_action}/reconciliations",
        headers={**headers(key="http-reconciliation"), "If-Match": f'"{unknown.version}"'},
        json={
            "action_id": unknown_action,
            "resolution": "confirmed_applied",
            "evidence_ids": ["operator-ticket-http-1"],
        },
    )
    assert reconciled.status_code == 202
    assert reconciled.headers["location"] == f"/v1/actions/{unknown_action}"
    assert controller.get_action(CONTEXT, unknown_action).status == ActionStatus.SUCCEEDED


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("second_resolution", "expected_second_status"),
    [
        ("confirmed_not_applied", ActionStatus.CANCELLED),
        ("confirmed_applied", ActionStatus.SUCCEEDED),
    ],
)
def test_reconciliation_cannot_cancel_run_with_another_unresolved_action(
    clean_postgres: Engine,
    second_resolution: str,
    expected_second_status: ActionStatus,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "p05-multiple-unknown-cancel")
    first = admitted_action(controller, run_id, "first-unknown", call_index=0)
    second = admitted_action(controller, run_id, "second-unknown", call_index=1)
    for action_id in (first, second):
        assert controller.begin_action_dispatch(action_id)
    for action_id in (first, second):
        controller.mark_action_outcome_unknown(
            action_id, receipt={"possibly_applied": True}, upstream_handle=None
        )
    controller.request_cancellation(
        CONTEXT, run_id, {"reason": "stop"}, "p05-multiple-unknown-stop"
    )
    first_snapshot = controller.get_action(CONTEXT, first)
    controller.reconcile_action(
        CONTEXT,
        first,
        expected_version=first_snapshot.version,
        resolution="confirmed_stopped",
        evidence_ids=["operator-confirmed-first-stop"],
        comment=None,
        idempotency_key="first-stop",
    )
    assert controller.get_action(CONTEXT, first).status == ActionStatus.CANCELLED
    assert controller.get_action(CONTEXT, second).status == ActionStatus.OUTCOME_UNKNOWN
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.CANCELLING
    second_snapshot = controller.get_action(CONTEXT, second)
    controller.reconcile_action(
        CONTEXT,
        second,
        expected_version=second_snapshot.version,
        resolution=second_resolution,
        evidence_ids=[f"operator-{second_resolution}"],
        comment=None,
        idempotency_key=f"second-{second_resolution}",
    )
    assert controller.get_action(CONTEXT, second).status == expected_second_status
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.CANCELLED
    with Session(clean_postgres) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRecord)
                .where(
                    EventRecord.run_id == run_id,
                    EventRecord.event_type == "run.cancelled",
                )
            )
            == 1
        )


@pytest.mark.postgres
def test_budget_termination_blocks_when_external_effect_is_unresolved(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "p05-budget")
    action_id = admitted_action(controller, run_id, "budget-running")
    assert controller.begin_action_dispatch(action_id)

    terminated = controller.terminate_run(
        run_id,
        code="budget_exhausted",
        detail="tool budget ended while an external action was running",
    )
    assert terminated.status == RunStatus.BLOCKED
    assert terminated.failure == {
        "code": "budget_exhausted",
        "detail": "tool budget ended while an external action was running",
        "termination_intent": True,
        "unresolved_effects": True,
    }


@pytest.mark.postgres
def test_runner_clarification_survives_restart_and_resumes_with_answer(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = running_run(controller, "p05-clarification")
    provider = ScriptedProvider(
        [
            {
                "public_output": "I need the report title.",
                "operations": [],
                "final_candidate": None,
                "request_input": "What title should the report use?",
            },
            {
                "public_output": "The answer was incorporated.",
                "operations": [],
                "final_candidate": None,
                "request_input": "Confirm the title one more time.",
            },
        ]
    )

    def make_runner() -> Runner:
        restarted = RunController(clean_postgres)
        registry = CapabilityRegistry()
        resources = ResourceStore(clean_postgres)
        gateway = ActionGateway(clean_postgres, registry, resources, restarted)
        return Runner(
            restarted,
            ContextBuilder(restarted, registry),
            gateway,
            CompletionGate(clean_postgres, restarted),
            provider,
        )

    waiting = make_runner().advance(CONTEXT, run_id)
    assert waiting.status == RunStatus.WAITING_INPUT
    with Session(clean_postgres) as session:
        first_request = session.scalar(
            select(InputRequestRecord).where(InputRequestRecord.run_id == run_id)
        )
        assert first_request is not None
        first_request_id = first_request.id

    restarted_controller = RunController(clean_postgres)
    answered = restarted_controller.respond_to_input(
        CONTEXT,
        first_request_id,
        expected_version=0,
        decision="submit",
        values={"title": "Quarterly safety review"},
        comment=None,
        idempotency_key="p05-clarification-answer",
    )
    assert answered.status == "answered"
    assert restarted_controller.get_run(CONTEXT, run_id).status == RunStatus.QUEUED

    resumed = make_runner().advance(CONTEXT, run_id)
    assert resumed.status == RunStatus.WAITING_INPUT
    assert provider.calls == 2
    with Session(clean_postgres) as session:
        second_context = session.scalar(
            select(ContextSnapshotRecord).where(
                ContextSnapshotRecord.run_id == run_id,
                ContextSnapshotRecord.turn_id == 2,
            )
        )
        assert second_context is not None
        assert "Quarterly safety review" in json.dumps(second_context.content)
