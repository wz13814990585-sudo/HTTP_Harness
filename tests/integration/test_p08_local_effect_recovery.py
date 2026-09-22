"""A leased worker may replay only native effects backed by PostgreSQL idempotency."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import (
    ActionAttemptRecord,
    ArtifactRecord,
    EventRecord,
    IdempotencyRecord,
    JobRecord,
)
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.run_worker import RunWorker
from hnh.application.runner import Runner
from hnh.domain.errors import LeaseLost, VersionConflict
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus

ACTOR = TrustedContext(
    "tenant-p08-local-effect",
    "subject-p08-local-effect",
    frozenset(
        {"runs:read", "workspace:read", "workspace:write", "artifacts:write", "artifacts:read"}
    ),
)


def _runner(
    engine: Engine,
    controller: RunController,
    provider: ScriptedProvider,
    resources: ResourceStore | None = None,
) -> Runner:
    registry = CapabilityRegistry()
    return Runner(
        controller,
        ContextBuilder(controller, registry),
        ActionGateway(engine, registry, resources or ResourceStore(engine), controller),
        CompletionGate(engine, controller),
        provider,
    )


@pytest.mark.postgres
def test_worker_commits_conditional_file_write_then_evidenced_completion(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {
            "agent_id": "p08-local-write",
            "input": "create a versioned file and report its committed receipt",
            "limits": {"max_model_turns": 2, "max_tool_calls": 1, "max_output_tokens": 2048},
        },
        "p08-local-write-run",
    ).resource_id
    first_provider = ScriptedProvider(
        [
            {
                "operations": [
                    {
                        "method": "PUT",
                        "target": "/v1/workspaces/ws-p08-local/files/result.txt",
                        "headers": {"content-type": "text/plain", "if-none-match": "*"},
                        "payload": {"kind": "text", "text": "one committed version"},
                    }
                ]
            }
        ]
    )
    first = RunWorker(
        controller,
        _runner(clean_postgres, controller, first_provider),
        lambda _tenant, _subject: ACTOR,
        worker_id="local-write-first",
    )
    step = first.run_once()
    assert step is not None and step.disposition == "deferred"
    action = controller.list_actions(ACTOR, run_id)[0]
    assert action.status == ActionStatus.SUCCEEDED
    assert action.downstream_idempotency_key == f"action:{action.id}"
    stored = ResourceStore(clean_postgres).read_file(ACTOR, "ws-p08-local", "result.txt")
    assert stored.content == b"one committed version"
    assert stored.snapshot.revision == 1
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.run_id == run_id, JobRecord.kind == "advance_run")
            .values(due_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    final_provider = ScriptedProvider(
        [
            {
                "final_candidate": {
                    "answer": "The file was written.",
                    "evidence_ids": [action.id],
                    "artifact_ids": [stored.snapshot.artifact_id],
                    "acceptance_claims": ["versioned file exists"],
                }
            }
        ]
    )
    final = RunWorker(
        controller,
        _runner(clean_postgres, controller, final_provider),
        lambda _tenant, _subject: ACTOR,
        worker_id="local-write-final",
    )
    finished = final.run_once()
    assert finished is not None and finished.disposition == "succeeded"
    assert controller.get_run(ACTOR, run_id).status == RunStatus.SUCCEEDED
    reread = ResourceStore(clean_postgres).read_file(ACTOR, "ws-p08-local", "result.txt")
    assert reread.snapshot.revision == 1


@pytest.mark.postgres
def test_reclaimed_worker_reuses_artifact_effect_after_receipt_commit_loss(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {
            "agent_id": "p08-local-recovery",
            "input": "store one artifact despite lost local Action receipt",
            "limits": {"max_model_turns": 1, "max_tool_calls": 1, "max_output_tokens": 2048},
        },
        "p08-local-recovery-run",
    ).resource_id
    first = controller.claim_job("local-old", lease_seconds=30, kind="advance_run")
    assert first is not None and first.run_id == run_id
    reclaimed: list[Any] = []

    class LoseReceiptAfterCommit(ResourceStore):
        def create_artifact(
            self,
            context: TrustedContext,
            content: bytes,
            media_type: str,
            idempotency_key: str,
            *,
            source_action_id: str | None = None,
            after_blob_before_metadata: Any = None,
        ) -> Any:
            receipt = super().create_artifact(
                context,
                content,
                media_type,
                idempotency_key,
                source_action_id=source_action_id,
                after_blob_before_metadata=after_blob_before_metadata,
            )
            assert receipt.source_action_id == source_action_id
            with Session(clean_postgres) as session, session.begin():
                session.execute(
                    update(JobRecord)
                    .where(JobRecord.id == first.id)
                    .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
                )
            successor = controller.claim_job("local-new", lease_seconds=30, kind="advance_run")
            assert successor is not None and successor.lease_epoch > first.lease_epoch
            reclaimed.append(successor)
            raise LeaseLost()

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
                                    b"one durable artifact"
                                ).decode(),
                                "media_type": "text/plain",
                            },
                        },
                    }
                ]
            }
        ]
    )
    old_runner = _runner(
        clean_postgres, controller, provider, LoseReceiptAfterCommit(clean_postgres)
    )
    with pytest.raises(LeaseLost):
        old_runner.advance(ACTOR, run_id, claimed_job=first, worker_id="local-old")
    action = controller.list_actions(ACTOR, run_id)[0]
    assert action.status == ActionStatus.RUNNING
    assert action.downstream_idempotency_key == f"action:{action.id}"
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(ArtifactRecord)) == 1
    successor = reclaimed[0]
    with pytest.raises(VersionConflict):
        controller.begin_action_dispatch(
            action.id,
            claimed_job=successor,
            worker_id="local-new",
            downstream_idempotency_key="changed-business-key",
        )
    new_provider = ScriptedProvider([])
    new_runner = _runner(clean_postgres, controller, new_provider)
    resumed = new_runner.advance(ACTOR, run_id, claimed_job=successor, worker_id="local-new")
    assert resumed.status == RunStatus.RUNNING
    assert new_provider.calls == 0
    completed = controller.list_actions(ACTOR, run_id)[0]
    assert completed.id == action.id and completed.status == ActionStatus.SUCCEEDED
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(ArtifactRecord)) == 1
        attempts = session.scalars(
            select(ActionAttemptRecord)
            .where(ActionAttemptRecord.action_id == action.id)
            .order_by(ActionAttemptRecord.attempt_no)
        ).all()
        assert [(row.attempt_no, row.status) for row in attempts] == [
            (1, "outcome_unknown"),
            (2, "succeeded"),
        ]
    with pytest.raises(LeaseLost):
        controller.complete_action(
            action.id,
            result={"status_code": 201, "value": {"id": "stale"}},
            succeeded=True,
            transport_kind="inproc",
            response_status=201,
            claimed_job=first,
            worker_id="local-old",
        )
    controller.complete_job(successor, "local-new")


@pytest.mark.postgres
def test_reclaimed_worker_replays_conditional_file_write_without_new_revision(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-file-recovery", "input": "create one versioned file"},
        "p08-file-recovery-run",
    ).resource_id
    controller.ensure_run_running(run_id, actor_ref="test")
    first = controller.claim_job("file-old", lease_seconds=30, kind="advance_run")
    assert first is not None and first.run_id == run_id
    reclaimed: list[Any] = []

    class LoseFileReceiptAfterCommit(ResourceStore):
        def write_file(
            self,
            context: TrustedContext,
            workspace_id: str,
            path: str,
            content: bytes,
            idempotency_key: str,
            *,
            if_match: str | None,
            if_none_match: str | None,
            source_action_id: str | None = None,
        ) -> Any:
            super().write_file(
                context,
                workspace_id,
                path,
                content,
                idempotency_key,
                if_match=if_match,
                if_none_match=if_none_match,
                source_action_id=source_action_id,
            )
            with Session(clean_postgres) as session, session.begin():
                session.execute(
                    update(JobRecord)
                    .where(JobRecord.id == first.id)
                    .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
                )
            successor = controller.claim_job("file-new", lease_seconds=30, kind="advance_run")
            assert successor is not None and successor.lease_epoch > first.lease_epoch
            reclaimed.append(successor)
            raise LeaseLost()

    operation = {
        "method": "PUT",
        "target": "/v1/workspaces/ws-p08-recovery/files/once.txt",
        "headers": {"content-type": "text/plain", "if-none-match": "*"},
        "payload": {"kind": "text", "text": "one version"},
    }
    old_gateway = ActionGateway(
        clean_postgres,
        CapabilityRegistry(),
        LoseFileReceiptAfterCommit(clean_postgres),
        controller,
    )
    with pytest.raises(LeaseLost):
        old_gateway.execute(
            ACTOR,
            operation,
            idempotency_key="outer-key-before-crash",
            transport_kind="inproc",
            run_id=run_id,
            turn_id=1,
            call_index=0,
            claimed_job=first,
            worker_id="file-old",
        )
    action = controller.list_actions(ACTOR, run_id)[0]
    assert action.status == ActionStatus.RUNNING
    successor = reclaimed[0]
    new_gateway = ActionGateway(
        clean_postgres, CapabilityRegistry(), ResourceStore(clean_postgres), controller
    )
    resumed = new_gateway.execute(
        ACTOR,
        operation,
        idempotency_key="changed-outer-key-but-same-action",
        transport_kind="inproc",
        run_id=run_id,
        turn_id=1,
        call_index=0,
        claimed_job=successor,
        worker_id="file-new",
    )
    assert resumed.succeeded and resumed.action_id == action.id
    assert resumed.replayed
    content = ResourceStore(clean_postgres).read_file(ACTOR, "ws-p08-recovery", "once.txt")
    assert content.content == b"one version"
    assert content.snapshot.revision == 1
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(ArtifactRecord)) == 1
    controller.complete_job(successor, "file-new")


@pytest.mark.postgres
@pytest.mark.parametrize("capability", ["artifact", "file"])
def test_cancel_only_worker_reconciles_committed_local_effect_without_replay(
    clean_postgres: Engine, capability: str
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-cancel-receipt", "input": "stop after a committed local effect"},
        f"p08-cancel-receipt-{capability}",
    ).resource_id
    old_job = controller.claim_job(f"p08-{capability}-old", kind="advance_run")
    assert old_job is not None

    class LoseActionReceipt(ResourceStore):
        def expire_original_lease(self) -> None:
            with Session(clean_postgres) as session, session.begin():
                session.execute(
                    update(JobRecord)
                    .where(JobRecord.id == old_job.id)
                    .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
                )

        def create_artifact(self, *args: Any, **kwargs: Any) -> Any:
            super().create_artifact(*args, **kwargs)
            self.expire_original_lease()
            raise LeaseLost()

        def write_file(self, *args: Any, **kwargs: Any) -> Any:
            super().write_file(*args, **kwargs)
            self.expire_original_lease()
            raise LeaseLost()

    operation = (
        {
            "method": "POST",
            "target": "/v1/artifacts",
            "headers": {"content-type": "application/octet-stream"},
            "payload": {
                "kind": "json",
                "value": {
                    "content_base64": base64.b64encode(b"cancel receipt").decode(),
                    "media_type": "text/plain",
                },
            },
        }
        if capability == "artifact"
        else {
            "method": "PUT",
            "target": "/v1/workspaces/ws-p08-cancel/files/once.txt",
            "headers": {"content-type": "text/plain", "if-none-match": "*"},
            "payload": {"kind": "text", "text": "cancel receipt"},
        }
    )
    gateway = ActionGateway(
        clean_postgres, CapabilityRegistry(), LoseActionReceipt(clean_postgres), controller
    )
    with pytest.raises(LeaseLost):
        gateway.execute(
            ACTOR,
            operation,
            idempotency_key=f"p08-lost-{capability}",
            transport_kind="inproc",
            run_id=run_id,
            turn_id=1,
            call_index=0,
            claimed_job=old_job,
            worker_id=f"p08-{capability}-old",
        )
    action = controller.list_actions(ACTOR, run_id)[0]
    assert action.status == ActionStatus.RUNNING
    controller.request_cancellation(
        ACTOR, run_id, {"reason": "user requested stop"}, f"p08-stop-{capability}"
    )
    stale_cancel = controller.claim_job(
        f"p08-{capability}-stale-cancel", lease_seconds=3, kind="cancel_run"
    )
    assert stale_cancel is not None
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.id == stale_cancel.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(LeaseLost):
        controller.reconcile_local_action_for_cancellation(
            ACTOR,
            action.id,
            claimed_job=stale_cancel,
            worker_id=f"p08-{capability}-stale-cancel",
        )
    assert controller.get_action(ACTOR, action.id).status == ActionStatus.RUNNING
    worker = RunWorker(
        controller,
        None,
        lambda _tenant, _subject: ACTOR,
        worker_id=f"p08-{capability}-cancel",
        cancellation_only=True,
    )
    step = worker.run_once()
    assert step is not None and step.disposition == "cancelled"
    assert controller.get_run(ACTOR, run_id).status == RunStatus.CANCELLED
    result = controller.get_action(ACTOR, action.id)
    assert result.status == ActionStatus.SUCCEEDED
    assert result.result is not None and result.result["status_code"] == 201
    with pytest.raises(LeaseLost):
        controller.complete_action(
            action.id,
            result={"status_code": 201, "value": {"id": "stale"}},
            succeeded=True,
            transport_kind="inproc",
            response_status=201,
            claimed_job=old_job,
            worker_id=f"p08-{capability}-old",
        )
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(ArtifactRecord)) == 1
        attempt = session.scalar(
            select(ActionAttemptRecord).where(ActionAttemptRecord.action_id == action.id)
        )
        assert attempt is not None and attempt.status == "succeeded"
        assert attempt.receipt is not None
        assert attempt.receipt["source"] == "local_idempotency_ledger"
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRecord)
                .where(
                    EventRecord.run_id == run_id,
                    EventRecord.event_type == "action.reconciled_local",
                )
            )
            == 1
        )
    if capability == "file":
        stored = ResourceStore(clean_postgres).read_file(ACTOR, "ws-p08-cancel", "once.txt")
        assert stored.snapshot.revision == 1
        assert stored.content == b"cancel receipt"


@pytest.mark.postgres
@pytest.mark.parametrize("receipt_state", ["missing", "mismatched"])
def test_cancel_only_worker_keeps_local_effect_unresolved_without_matching_receipt(
    clean_postgres: Engine, receipt_state: str
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {"agent_id": "p08-cancel-unresolved", "input": "stop uncertain local effect"},
        f"p08-cancel-unresolved-{receipt_state}",
    ).resource_id
    old_job = controller.claim_job(f"p08-{receipt_state}-old", kind="advance_run")
    assert old_job is not None

    class UncertainStore(ResourceStore):
        def create_artifact(self, *args: Any, **kwargs: Any) -> Any:
            if receipt_state != "missing":
                super().create_artifact(*args, **kwargs)
            with Session(clean_postgres) as session, session.begin():
                session.execute(
                    update(JobRecord)
                    .where(JobRecord.id == old_job.id)
                    .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
                )
            raise LeaseLost()

    gateway = ActionGateway(
        clean_postgres, CapabilityRegistry(), UncertainStore(clean_postgres), controller
    )
    with pytest.raises(LeaseLost):
        gateway.execute(
            ACTOR,
            {
                "method": "POST",
                "target": "/v1/artifacts",
                "headers": {"content-type": "application/octet-stream"},
                "payload": {
                    "kind": "json",
                    "value": {
                        "content_base64": base64.b64encode(b"uncertain").decode(),
                        "media_type": "text/plain",
                    },
                },
            },
            idempotency_key=f"p08-uncertain-{receipt_state}",
            transport_kind="inproc",
            run_id=run_id,
            turn_id=1,
            call_index=0,
            claimed_job=old_job,
            worker_id=f"p08-{receipt_state}-old",
        )
    action = controller.list_actions(ACTOR, run_id)[0]
    if receipt_state == "mismatched":
        with Session(clean_postgres) as session, session.begin():
            record = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.idempotency_key == f"action:{action.id}"
                )
            )
            assert record is not None
            record.request_hash = "0" * 64
    controller.request_cancellation(
        ACTOR, run_id, {"reason": "user requested stop"}, f"p08-stop-{receipt_state}"
    )
    worker = RunWorker(
        controller,
        None,
        lambda _tenant, _subject: ACTOR,
        worker_id=f"p08-{receipt_state}-cancel",
        cancellation_only=True,
    )
    step = worker.run_once()
    assert step is not None and step.disposition == "awaiting_cancellation_evidence"
    assert controller.get_run(ACTOR, run_id).status == RunStatus.CANCELLING
    assert controller.get_action(ACTOR, action.id).status == ActionStatus.RUNNING
