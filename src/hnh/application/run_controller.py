from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from hnh.adapters.postgres.database import build_session_factory
from hnh.adapters.postgres.models import (
    ActionAttemptRecord,
    ActionRecord,
    ArtifactRecord,
    BudgetRecord,
    BudgetReservationRecord,
    CheckpointRecord,
    CompletionRecord,
    ContextSnapshotRecord,
    EventRecord,
    ExecutionRecord,
    ExecutionSessionRecord,
    HttpExchangeRecord,
    IdempotencyRecord,
    InputRequestRecord,
    InputResponseRecord,
    JobRecord,
    MCPContinuationRecord,
    MCPRPCRecord,
    MCPTaskRecord,
    ModelCallAttemptRecord,
    ModelCallRecord,
    ReconciliationRecord,
    RunRecord,
)
from hnh.application.security import redact, scope_fingerprint
from hnh.domain.errors import (
    AlreadyTerminal,
    ApprovalRequired,
    BudgetExhausted,
    EnvironmentLost,
    EventCursorExpired,
    ExecutionBusy,
    FutureEventCursor,
    IdempotencyConflict,
    InputRequestExpired,
    InvalidOperation,
    LeaseLost,
    MigrationRequired,
    PermissionDenied,
    ResourceNotFound,
    VersionConflict,
)
from hnh.domain.identity import TrustedContext
from hnh.domain.states import (
    TERMINAL_RUN_STATUSES,
    ActionStatus,
    RunStatus,
    require_run_transition,
)

FaultHook = Callable[[], None]
LEASE_REPLAYABLE_LOCAL_CAPABILITIES = frozenset({"artifact.create", "workspace.file.write"})


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def request_fingerprint(method: str, route: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"method": method, "route": route, "payload": payload},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class Accepted:
    resource_id: str
    location: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    id: str
    status: RunStatus
    version: int
    agent_id: str
    conversation_id: str | None
    created_at: datetime
    updated_at: datetime
    effective_limits: dict[str, int]
    workspace_id: str | None
    goal: str
    artifact_refs: tuple[str, ...]
    skill_refs: tuple[dict[str, str], ...] | None
    parent_run_id: str | None
    child_depth: int
    admitted_scopes: frozenset[str]
    delegated_scopes: frozenset[str] | None
    result_artifact_ids: tuple[str, ...]
    final_answer: str | None
    failure: dict[str, Any] | None
    event_seq: int


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    kind: str
    run_id: str | None
    action_id: str | None
    lease_epoch: int


@dataclass(frozen=True, slots=True)
class ActionSnapshot:
    id: str
    run_id: str
    status: ActionStatus
    request_hash: str
    capability_id: str
    capability_revision: str
    effect_semantics: str
    result: dict[str, Any] | None
    version: int
    turn_id: int
    call_index: int
    bound_operation: dict[str, Any]
    policy_revision: str
    scope_fingerprint: str
    approval_request_id: str | None
    downstream_idempotency_key: str | None
    upstream_handle: str | None
    lease_epoch: int


@dataclass(frozen=True, slots=True)
class InputRequestSnapshot:
    id: str
    run_id: str
    action_id: str | None
    kind: str
    status: str
    version: int
    prompt: str
    request_hash: str
    requested_schema: dict[str, Any]
    expires_at: datetime
    target_summary: str | None
    capability_revision: str | None
    policy_revision: str
    scope_fingerprint: str
    resource_versions: dict[str, Any]

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "version": self.version,
            "prompt": self.prompt,
            "request_hash": self.request_hash,
            "requested_schema": self.requested_schema,
            "expires_at": self.expires_at.isoformat(),
        }
        if self.action_id is not None:
            result["action_id"] = self.action_id
        if self.target_summary is not None:
            result["target_summary"] = self.target_summary
        return result


@dataclass(frozen=True, slots=True)
class CheckpointSnapshot:
    id: str
    run_id: str
    checkpoint_seq: int
    schema_version: str
    capability_id: str | None
    capability_revision: str | None
    policy_revision: str
    runner_cursor: dict[str, Any]
    content: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MCPRPCSnapshot:
    id: str
    action_id: str
    method: str
    sequence: int
    correlation_id: str
    status: str


@dataclass(frozen=True, slots=True)
class MCPContinuationSnapshot:
    id: str
    action_id: str
    input_request_id: str
    integration_id: str
    profile: str
    tool_name: str
    sequence: int
    request_state: str | None
    input_requests: dict[str, Any]
    status: str
    response_values: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class MCPTaskSnapshot:
    id: str
    action_id: str
    integration_id: str
    profile: str
    task_id: str
    status: str
    version: int
    created_at_remote: str
    last_updated_at_remote: str
    ttl_ms: int | None
    poll_interval_ms: int | None
    input_requests: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    cancel_acknowledged: bool


@dataclass(frozen=True, slots=True)
class ActionAdmission:
    action: ActionSnapshot
    replayed: bool


@dataclass(frozen=True, slots=True)
class ModelCallSnapshot:
    id: str
    run_id: str
    turn_id: int
    provider: str
    model_revision: str
    status: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None
    parsed_output: Any | None
    response_hash: str | None
    provider_request_id: str | None
    usage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelCallWork:
    call: ModelCallSnapshot
    attempt_id: str | None
    attempt_no: int | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    event_id: str
    run_id: str
    seq: int
    schema_version: str
    type: str
    occurred_at: datetime
    action_id: str | None
    data: dict[str, Any]

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "schema_version": self.schema_version,
            "type": self.type,
            "occurred_at": self.occurred_at.isoformat(),
            "data": self.data,
        }
        if self.action_id is not None:
            result["action_id"] = self.action_id
        return result


@dataclass(frozen=True, slots=True)
class EventPageSnapshot:
    events: tuple[EventSnapshot, ...]
    next_after: int | None


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    id: str
    run_id: str
    dimension: str
    reserved_amount: int
    status: str


@dataclass(frozen=True, slots=True)
class SavedContextSnapshot:
    id: str
    run_id: str
    turn_id: int
    content: dict[str, Any]
    source_refs: tuple[dict[str, Any], ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class ExecutionSessionSnapshot:
    id: str
    run_id: str
    runtime: str
    profile_id: str
    generation: str
    status: str
    sandbox_ref: str | None
    cell_seq: int
    version: int
    expires_at: datetime | None

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "run_id": self.run_id,
            "generation": self.generation,
            "status": self.status,
            "runtime": self.runtime,
        }
        if self.expires_at is not None:
            result["expires_at"] = self.expires_at.isoformat()
        return result


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    id: str
    session_id: str
    action_id: str | None
    generation: str
    cell_index: int
    request_hash: str
    code_hash: str
    status: str
    receipt: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ExecutionWork:
    execution: ExecutionSnapshot
    replayed: bool


class RunController:
    """The sole P01 state-mutation service for Runs and dispatch admission."""

    def __init__(self, engine: Engine) -> None:
        self._sessions: sessionmaker[Session] = build_session_factory(engine)

    def admit_run(
        self,
        context: TrustedContext,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        before_commit: FaultHook | None = None,
    ) -> Accepted:
        method = "POST"
        route = "/v1/runs"
        fingerprint = request_fingerprint(method, route, payload)
        run_id = _identifier("run")
        location = f"/v1/runs/{run_id}"
        response = {"resource_id": run_id, "location": location}

        with self._sessions.begin() as session:
            inserted = session.scalar(
                insert(IdempotencyRecord)
                .values(
                    id=_identifier("idem"),
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    method=method,
                    canonical_route=route,
                    idempotency_key=idempotency_key,
                    request_hash=fingerprint,
                    response_status=202,
                    resource_id=run_id,
                    location=location,
                    response_body=response,
                )
                .on_conflict_do_nothing(constraint="uq_idempotency_records_request_scope")
                .returning(IdempotencyRecord.id)
            )
            if inserted is None:
                existing = session.scalar(
                    self._idempotency_query(
                        context, method, route, idempotency_key
                    ).with_for_update()
                )
                if existing is None:
                    raise RuntimeError("idempotency conflict row disappeared")
                if existing.request_hash != fingerprint:
                    raise IdempotencyConflict()
                return Accepted(existing.resource_id, existing.location, True)

            now = datetime.now(UTC)
            limits = dict(payload.get("limits") or {})
            run = RunRecord(
                id=run_id,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                agent_id=payload["agent_id"],
                conversation_id=payload.get("conversation_id"),
                workspace_id=payload.get("workspace_id"),
                goal=payload["input"],
                admitted_scopes=sorted(context.scopes),
                artifact_refs=list(payload.get("artifact_refs") or []),
                effective_limits=limits,
                status=RunStatus.QUEUED.value,
                version=0,
                event_seq=1,
                created_at=now,
                updated_at=now,
            )
            session.add(run)
            # The records below reference Run but deliberately have no ORM
            # relationships. Flush the aggregate root first so PostgreSQL can
            # enforce every foreign key while the encompassing transaction still
            # commits or rolls back atomically.
            session.flush()
            session.add(
                EventRecord(
                    id=_identifier("evt"),
                    run_id=run_id,
                    seq=1,
                    event_type="run.accepted",
                    actor_ref=context.subject_id,
                    data={"status": RunStatus.QUEUED.value},
                )
            )
            session.add(
                JobRecord(
                    id=_identifier("job"),
                    tenant_id=context.tenant_id,
                    kind="advance_run",
                    run_id=run_id,
                    dedupe_key=f"run:{run_id}:initial",
                    status="ready",
                    due_at=now,
                )
            )
            session.add(
                BudgetRecord(
                    run_id=run_id,
                    max_model_turns=limits.get("max_model_turns"),
                    max_tool_calls=limits.get("max_tool_calls"),
                    max_wall_seconds=limits.get("max_wall_seconds"),
                    max_output_tokens=limits.get("max_output_tokens"),
                    max_cost_microunits=limits.get("max_cost_microunits"),
                )
            )
            if before_commit is not None:
                session.flush()
                before_commit()

        return Accepted(run_id, location, False)

    def get_run(self, context: TrustedContext, run_id: str) -> RunSnapshot:
        with self._sessions() as session:
            run = session.scalar(
                select(RunRecord).where(
                    RunRecord.id == run_id,
                    RunRecord.tenant_id == context.tenant_id,
                    RunRecord.subject_id == context.subject_id,
                )
            )
            if run is None:
                raise ResourceNotFound("run")
            return self._snapshot(run)

    def effective_context(self, context: TrustedContext, run_id: str) -> TrustedContext:
        run = self.get_run(context, run_id)
        scopes = context.scopes.intersection(run.admitted_scopes)
        if run.delegated_scopes is not None:
            scopes = scopes.intersection(run.delegated_scopes)
        return TrustedContext(context.tenant_id, context.subject_id, frozenset(scopes))

    def admit_child_run(
        self,
        context: TrustedContext,
        parent_run_id: str,
        *,
        goal: str,
        requested_scopes: frozenset[str],
        limits: dict[str, int],
        idempotency_key: str,
        max_depth: int = 2,
        max_children: int = 4,
    ) -> Accepted:
        """Internal-only child admission; lineage and quota reservation commit together."""
        if not goal.strip() or max_depth < 1 or max_children < 1:
            raise InvalidOperation("invalid child goal or bounds")
        dimensions = {
            "max_model_turns": ("model_turn", "max_model_turns", "used_model_turns"),
            "max_tool_calls": ("tool_call", "max_tool_calls", "used_tool_calls"),
            "max_output_tokens": ("output_tokens", "max_output_tokens", "used_output_tokens"),
            "max_cost_microunits": (
                "cost_microunits",
                "max_cost_microunits",
                "used_cost_microunits",
            ),
        }
        required_limits = {"max_model_turns", "max_tool_calls", "max_output_tokens"}
        allowed_limits = set(dimensions) | {"max_wall_seconds"}
        if (
            not required_limits.issubset(limits)
            or not set(limits).issubset(allowed_limits)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in limits.values()
            )
        ):
            raise InvalidOperation("child requires positive finite model/tool/output budgets")
        route = "/internal/runs/{run_id}/children"
        payload = {
            "parent_run_id": parent_run_id,
            "goal": goal,
            "requested_scopes": sorted(requested_scopes),
            "limits": limits,
        }
        fingerprint = request_fingerprint("POST", route, payload)
        child_id = _identifier("run")
        location = f"/v1/runs/{child_id}"
        with self._sessions.begin() as session:
            parent = self._owned_run_locked(session, context, parent_run_id)
            existing = session.scalar(
                self._idempotency_query(context, "POST", route, idempotency_key).with_for_update()
            )
            if existing is not None:
                if existing.request_hash != fingerprint:
                    raise IdempotencyConflict()
                return Accepted(existing.resource_id, existing.location, True)
            if RunStatus(parent.status) not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                raise InvalidOperation("parent is not accepting new child work")
            if parent.child_depth >= max_depth:
                raise InvalidOperation("child depth limit reached")
            count = session.scalar(
                select(func.count())
                .select_from(RunRecord)
                .where(RunRecord.parent_run_id == parent.id)
            )
            if int(count or 0) >= max_children:
                raise InvalidOperation("child count limit reached")
            parent_scopes = context.scopes.intersection(parent.admitted_scopes)
            if parent.delegated_scopes is not None:
                parent_scopes = parent_scopes.intersection(parent.delegated_scopes)
            child_scopes = sorted(parent_scopes.intersection(requested_scopes))
            budget = session.scalar(
                select(BudgetRecord).where(BudgetRecord.run_id == parent.id).with_for_update()
            )
            if budget is None:
                raise ResourceNotFound("parent budget")
            if budget.max_cost_microunits is not None and "max_cost_microunits" not in limits:
                raise InvalidOperation("child must reserve a finite cost quota")
            if budget.max_wall_seconds is not None:
                child_wall = limits.get("max_wall_seconds")
                remaining_wall = budget.max_wall_seconds - int(
                    (datetime.now(UTC) - parent.created_at).total_seconds()
                )
                if child_wall is None or child_wall > remaining_wall:
                    raise InvalidOperation("child wall-time limit must fit parent limit")
            for key, (dimension, maximum, used_name) in dimensions.items():
                if key not in limits:
                    continue
                amount = limits[key]
                active = session.scalar(
                    select(
                        func.coalesce(func.sum(BudgetReservationRecord.reserved_amount), 0)
                    ).where(
                        BudgetReservationRecord.run_id == parent.id,
                        BudgetReservationRecord.dimension == dimension,
                        BudgetReservationRecord.status == "reserved",
                    )
                )
                cap = getattr(budget, maximum)
                total = int(getattr(budget, used_name)) + int(active or 0) + amount
                if cap is not None and total > cap:
                    raise BudgetExhausted(dimension)
            inserted = session.scalar(
                insert(IdempotencyRecord)
                .values(
                    id=_identifier("idem"),
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    method="POST",
                    canonical_route=route,
                    idempotency_key=idempotency_key,
                    request_hash=fingerprint,
                    response_status=202,
                    resource_id=child_id,
                    location=location,
                    response_body={"resource_id": child_id, "location": location},
                )
                .on_conflict_do_nothing(constraint="uq_idempotency_records_request_scope")
                .returning(IdempotencyRecord.id)
            )
            if inserted is None:
                raise IdempotencyConflict()
            now = datetime.now(UTC)
            child = RunRecord(
                id=child_id,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                agent_id=parent.agent_id,
                conversation_id=parent.conversation_id,
                workspace_id=parent.workspace_id,
                goal=goal,
                admitted_scopes=child_scopes,
                artifact_refs=list(parent.artifact_refs),
                effective_limits=dict(limits),
                parent_run_id=parent.id,
                child_depth=parent.child_depth + 1,
                delegated_scopes=child_scopes,
                status=RunStatus.QUEUED.value,
                version=0,
                event_seq=1,
                policy_revision=parent.policy_revision,
                created_at=now,
                updated_at=now,
            )
            session.add(child)
            session.flush()
            for key, (dimension, _maximum, _used_name) in dimensions.items():
                if key not in limits:
                    continue
                session.add(
                    BudgetReservationRecord(
                        id=_identifier("reservation"),
                        run_id=parent.id,
                        reservation_key=f"child:{child_id}:{dimension}",
                        dimension=dimension,
                        reserved_amount=limits[key],
                        status="reserved",
                    )
                )
            session.add(
                BudgetRecord(
                    run_id=child_id,
                    max_model_turns=limits["max_model_turns"],
                    max_tool_calls=limits["max_tool_calls"],
                    max_output_tokens=limits["max_output_tokens"],
                    max_cost_microunits=limits.get("max_cost_microunits"),
                    max_wall_seconds=limits.get("max_wall_seconds"),
                )
            )
            session.add(
                EventRecord(
                    id=_identifier("evt"),
                    run_id=child_id,
                    seq=1,
                    event_type="run.accepted",
                    actor_ref="kernel",
                    data={"status": RunStatus.QUEUED.value, "parent_run_id": parent.id},
                )
            )
            session.add(
                JobRecord(
                    id=_identifier("job"),
                    tenant_id=context.tenant_id,
                    kind="advance_run",
                    run_id=child_id,
                    dedupe_key=f"run:{child_id}:initial",
                    status="ready",
                    due_at=now,
                )
            )
            self._append_event_locked(
                session,
                parent,
                "run.child_accepted",
                "kernel",
                {"child_run_id": child_id, "depth": child.child_depth, "scopes": child_scopes},
            )
        return Accepted(child_id, location, False)

    def pin_run_skills(
        self,
        context: TrustedContext,
        run_id: str,
        refs: list[dict[str, str]],
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> RunSnapshot:
        """Pin exact skill revisions once; later turns may not silently swap guidance."""
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = session.scalar(
                select(RunRecord)
                .where(
                    RunRecord.id == run_id,
                    RunRecord.tenant_id == context.tenant_id,
                    RunRecord.subject_id == context.subject_id,
                )
                .with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            if run.skill_refs is None:
                run.skill_refs = refs
                run.version += 1
                run.updated_at = datetime.now(UTC)
                run.event_seq += 1
                session.add(
                    EventRecord(
                        id=_identifier("evt"),
                        run_id=run_id,
                        seq=run.event_seq,
                        event_type="run.skills_pinned",
                        actor_ref="kernel",
                        data={"skills": refs},
                    )
                )
            elif run.skill_refs != refs:
                raise MigrationRequired("approved skill revision changed during this run")
            return self._snapshot(run)

    def request_cancellation(
        self,
        context: TrustedContext,
        run_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> Accepted:
        method = "POST"
        route = "/v1/runs/{run_id}/cancellations"
        fingerprint = request_fingerprint(method, route, {"run_id": run_id, "body": payload})
        location = f"/v1/runs/{run_id}"
        response = {"resource_id": run_id, "location": location}

        with self._sessions.begin() as session:
            run = session.scalar(
                select(RunRecord)
                .where(
                    RunRecord.id == run_id,
                    RunRecord.tenant_id == context.tenant_id,
                    RunRecord.subject_id == context.subject_id,
                )
                .with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")

            inserted = session.scalar(
                insert(IdempotencyRecord)
                .values(
                    id=_identifier("idem"),
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    method=method,
                    canonical_route=route,
                    idempotency_key=idempotency_key,
                    request_hash=fingerprint,
                    response_status=202,
                    resource_id=run_id,
                    location=location,
                    response_body=response,
                )
                .on_conflict_do_nothing(constraint="uq_idempotency_records_request_scope")
                .returning(IdempotencyRecord.id)
            )
            if inserted is None:
                existing = session.scalar(
                    self._idempotency_query(
                        context, method, route, idempotency_key
                    ).with_for_update()
                )
                if existing is None:
                    raise RuntimeError("idempotency conflict row disappeared")
                if existing.request_hash != fingerprint:
                    raise IdempotencyConflict()
                return Accepted(existing.resource_id, existing.location, True)

            current = RunStatus(run.status)
            if current in TERMINAL_RUN_STATUSES:
                raise AlreadyTerminal()
            if current != RunStatus.CANCELLING:
                require_run_transition(current, RunStatus.CANCELLING)
                run.status = RunStatus.CANCELLING.value
                run.version += 1
                run.event_seq += 1
                run.cancel_reason = redact(payload.get("reason"))
                run.updated_at = datetime.now(UTC)
                session.execute(
                    update(ActionRecord)
                    .where(
                        ActionRecord.run_id == run.id,
                        ActionRecord.status.in_(
                            {
                                ActionStatus.PROPOSED.value,
                                ActionStatus.WAITING_INPUT.value,
                                ActionStatus.READY.value,
                            }
                        ),
                    )
                    .values(status=ActionStatus.CANCELLED.value)
                )
                session.add(
                    EventRecord(
                        id=_identifier("evt"),
                        run_id=run.id,
                        seq=run.event_seq,
                        event_type="run.cancellation_requested",
                        actor_ref=context.subject_id,
                        data={"reason": run.cancel_reason},
                    )
                )
                session.add(
                    JobRecord(
                        id=_identifier("job"),
                        tenant_id=context.tenant_id,
                        kind="cancel_run",
                        run_id=run.id,
                        dedupe_key=f"run:{run.id}:cancel:{run.version}",
                        status="ready",
                        due_at=datetime.now(UTC),
                    )
                )
                # The parent cancellation intent is committed with every live
                # descendant's intent. A running effect remains unresolved until
                # its own cancellation/reconciliation path confirms the outcome.
                pending = {run.id}
                descendants = session.scalars(
                    select(RunRecord)
                    .where(
                        RunRecord.tenant_id == context.tenant_id,
                        RunRecord.subject_id == context.subject_id,
                        RunRecord.parent_run_id.is_not(None),
                    )
                    .order_by(RunRecord.child_depth, RunRecord.id)
                    .with_for_update()
                ).all()
                for child in descendants:
                    if child.parent_run_id not in pending:
                        continue
                    pending.add(child.id)
                    child_status = RunStatus(child.status)
                    if (
                        child_status in TERMINAL_RUN_STATUSES
                        or child_status == RunStatus.CANCELLING
                    ):
                        continue
                    require_run_transition(child_status, RunStatus.CANCELLING)
                    child.status = RunStatus.CANCELLING.value
                    child.version += 1
                    child.cancel_reason = run.cancel_reason
                    child.updated_at = datetime.now(UTC)
                    session.execute(
                        update(ActionRecord)
                        .where(
                            ActionRecord.run_id == child.id,
                            ActionRecord.status.in_(
                                {
                                    ActionStatus.PROPOSED.value,
                                    ActionStatus.WAITING_INPUT.value,
                                    ActionStatus.READY.value,
                                }
                            ),
                        )
                        .values(status=ActionStatus.CANCELLED.value)
                    )
                    self._append_event_locked(
                        session,
                        child,
                        "run.cancellation_requested",
                        context.subject_id,
                        {"reason": child.cancel_reason, "propagated_from": run.id},
                    )
                    session.add(
                        JobRecord(
                            id=_identifier("job"),
                            tenant_id=context.tenant_id,
                            kind="cancel_run",
                            run_id=child.id,
                            dedupe_key=f"run:{child.id}:cancel:{child.version}",
                            status="ready",
                            due_at=datetime.now(UTC),
                        )
                    )

        return Accepted(run_id, location, False)

    def finalize_run_cancellation(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> RunSnapshot:
        """Confirm a cancellation only when effects and descendants have settled."""
        with self._sessions.begin() as session:
            self._require_claimed_cancel_job_locked(session, claimed_job, worker_id, run_id)
            run = self._owned_run_locked(session, context, run_id)
            if RunStatus(run.status) == RunStatus.CANCELLED:
                return self._snapshot(run)
            if RunStatus(run.status) != RunStatus.CANCELLING:
                raise VersionConflict()
            if self._has_unresolved_actions(session, run_id) or self._has_nonterminal_children(
                session, run_id
            ):
                raise VersionConflict()
            require_run_transition(RunStatus.CANCELLING, RunStatus.CANCELLED)
            run.status = RunStatus.CANCELLED.value
            run.version += 1
            run.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                "run.cancelled",
                "kernel",
                {"status": RunStatus.CANCELLED.value, "version": run.version},
            )
            session.execute(
                update(JobRecord)
                .where(JobRecord.run_id == run_id, JobRecord.status == "ready")
                .values(status="done")
            )
            session.flush()
            return self._snapshot(run)

    def reconcile_local_action_for_cancellation(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        claimed_job: ClaimedJob,
        worker_id: str,
    ) -> bool:
        """Confirm a committed local effect from its DB receipt without dispatching it again.

        A missing or inconsistent receipt is not evidence that the effect did not
        happen. In that case the Action remains unresolved and cancellation waits.
        """
        if claimed_job.run_id is None:
            raise LeaseLost()
        with self._sessions.begin() as session:
            self._require_claimed_cancel_job_locked(
                session, claimed_job, worker_id, claimed_job.run_id
            )
            action = self._owned_action_locked(session, context, action_id)
            if action.run_id != claimed_job.run_id:
                raise LeaseLost()
            if ActionStatus(action.status) == ActionStatus.SUCCEEDED:
                return True
            if (
                ActionStatus(action.status) != ActionStatus.RUNNING
                or action.effect_semantics != "local_transactional"
                or action.capability_id not in LEASE_REPLAYABLE_LOCAL_CAPABILITIES
                or action.downstream_idempotency_key != f"action:{action.id}"
            ):
                return False
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None or RunStatus(run.status) != RunStatus.CANCELLING:
                raise VersionConflict()
            try:
                parameters = action.bound_operation
                if action.capability_id == "artifact.create":
                    content = base64.b64decode(parameters["content_base64"], validate=True)
                    media_type = parameters["media_type"]
                    method, route = "POST", "/v1/artifacts"
                    fingerprint_payload = {
                        "media_type": media_type,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    allowed_statuses = {201}
                else:
                    content = parameters["text"].encode()
                    workspace_id = parameters["workspace_id"]
                    file_path = parameters["file_path"]
                    method = "PUT"
                    route = f"/v1/workspaces/{workspace_id}/files/{file_path}"
                    fingerprint_payload = {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "if_match": parameters.get("if_match"),
                        "if_none_match": parameters.get("if_none_match"),
                    }
                    allowed_statuses = {200, 201}
            except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
                return False
            expected_hash = request_fingerprint(method, route, fingerprint_payload)
            receipt = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == context.tenant_id,
                    IdempotencyRecord.subject_id == context.subject_id,
                    IdempotencyRecord.method == method,
                    IdempotencyRecord.canonical_route == route,
                    IdempotencyRecord.idempotency_key == action.downstream_idempotency_key,
                )
            )
            if (
                receipt is None
                or receipt.request_hash != expected_hash
                or receipt.response_status not in allowed_statuses
                or not isinstance(receipt.response_body, dict)
            ):
                return False
            artifact = session.scalar(
                select(ArtifactRecord).where(
                    ArtifactRecord.id == receipt.resource_id,
                    ArtifactRecord.tenant_id == context.tenant_id,
                    ArtifactRecord.subject_id == context.subject_id,
                    ArtifactRecord.source_action_id == action.id,
                    ArtifactRecord.sha256 == fingerprint_payload["sha256"],
                )
            )
            if artifact is None or artifact.size_bytes != len(content):
                return False
            body = receipt.response_body
            if action.capability_id == "artifact.create":
                if (
                    artifact.media_type != media_type
                    or body.get("id") != artifact.id
                    or body.get("source_action_id") != action.id
                    or body.get("sha256") != artifact.sha256
                ):
                    return False
            else:
                if (
                    artifact.media_type != "text/plain"
                    or body.get("artifact_id") != artifact.id
                    or body.get("workspace_id") != workspace_id
                    or body.get("path") != file_path
                    or not isinstance(body.get("revision"), int)
                    or body["revision"] < 1
                    or body.get("etag") != f'"sha256:{artifact.sha256}:r{body["revision"]}"'
                ):
                    return False
            attempt = self._latest_attempt_locked(session, action.id)
            if attempt.downstream_idempotency_key != action.downstream_idempotency_key:
                return False
            now = datetime.now(UTC)
            action.status = ActionStatus.SUCCEEDED.value
            action.result = {"status_code": receipt.response_status, "value": body}
            action.version += 1
            attempt.status = "succeeded"
            attempt.receipt = {"source": "local_idempotency_ledger", "record_id": receipt.id}
            attempt.completed_at = now
            self._append_event_locked(
                session,
                run,
                "action.reconciled_local",
                "kernel",
                {"action_id": action.id, "idempotency_record_id": receipt.id},
                action_id=action.id,
            )
            session.flush()
            return True

    def transition_run(
        self,
        run_id: str,
        expected_version: int,
        new_status: RunStatus,
        *,
        event_type: str,
        actor_ref: str,
        after_snapshot_before_event: FaultHook | None = None,
    ) -> RunSnapshot:
        with self._sessions.begin() as session:
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
            if run is None:
                raise ResourceNotFound("run")
            if run.version != expected_version:
                raise VersionConflict()
            require_run_transition(RunStatus(run.status), new_status)
            if new_status in TERMINAL_RUN_STATUSES and self._has_nonterminal_children(
                session, run_id
            ):
                raise VersionConflict()
            run.status = new_status.value
            run.version += 1
            run.event_seq += 1
            run.updated_at = datetime.now(UTC)
            session.flush()
            if after_snapshot_before_event is not None:
                after_snapshot_before_event()
            session.add(
                EventRecord(
                    id=_identifier("evt"),
                    run_id=run.id,
                    seq=run.event_seq,
                    event_type=event_type,
                    actor_ref=actor_ref,
                    data={"status": new_status.value, "version": run.version},
                )
            )
            session.flush()
            snapshot = self._snapshot(run)
        return snapshot

    def claim_job(
        self,
        worker_id: str,
        lease_seconds: int = 30,
        *,
        kind: str | None = None,
    ) -> ClaimedJob | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._sessions.begin() as session:
            now = session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database clock is unavailable")
            job = session.scalar(
                select(JobRecord)
                .where(
                    or_(
                        JobRecord.status == "ready",
                        (JobRecord.status == "leased") & (JobRecord.lease_until < func.now()),
                    ),
                    JobRecord.due_at <= func.now(),
                    *([JobRecord.kind == kind] if kind is not None else []),
                )
                .order_by(JobRecord.due_at, JobRecord.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = "leased"
            job.lease_owner = worker_id
            job.lease_until = now + timedelta(seconds=lease_seconds)
            job.lease_epoch += 1
            job.attempts += 1
            session.flush()
            return ClaimedJob(job.id, job.kind, job.run_id, job.action_id, job.lease_epoch)

    def renew_job_lease(
        self,
        claimed: ClaimedJob,
        worker_id: str,
        *,
        lease_seconds: int = 30,
    ) -> ClaimedJob:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._sessions.begin() as session:
            now = session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database clock is unavailable")
            job = session.scalar(
                select(JobRecord).where(JobRecord.id == claimed.id).with_for_update()
            )
            if not self._lease_matches(job, claimed, worker_id, now=now):
                raise LeaseLost()
            assert job is not None
            job.lease_until = now + timedelta(seconds=lease_seconds)
            session.flush()
            return ClaimedJob(job.id, job.kind, job.run_id, job.action_id, job.lease_epoch)

    def complete_job(self, claimed: ClaimedJob, worker_id: str) -> None:
        with self._sessions.begin() as session:
            now = session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database clock is unavailable")
            job = session.scalar(
                select(JobRecord).where(JobRecord.id == claimed.id).with_for_update()
            )
            if not self._lease_matches(job, claimed, worker_id, now=now):
                raise LeaseLost()
            assert job is not None
            job.status = "done"
            job.lease_owner = None
            job.lease_until = None

    def defer_job(self, claimed: ClaimedJob, worker_id: str, *, delay_seconds: int) -> None:
        """Release an owned lease to a future durable wake-up, never a hot retry."""
        if delay_seconds <= 0:
            raise ValueError("delay_seconds must be positive")
        with self._sessions.begin() as session:
            now = session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database clock is unavailable")
            job = session.scalar(
                select(JobRecord).where(JobRecord.id == claimed.id).with_for_update()
            )
            if not self._lease_matches(job, claimed, worker_id, now=now):
                raise LeaseLost()
            assert job is not None
            job.status = "ready"
            job.due_at = now + timedelta(seconds=delay_seconds)
            job.lease_owner = None
            job.lease_until = None

    def claimed_run_actor(self, claimed: ClaimedJob, worker_id: str) -> tuple[str, str]:
        """Load identity for a leased Run; never use its saved scopes as authority."""
        if claimed.kind not in {"advance_run", "cancel_run"} or claimed.run_id is None:
            raise InvalidOperation("an advance_run or cancel_run job is required")
        with self._sessions.begin() as session:
            if claimed.kind == "advance_run":
                self._require_claimed_run_job_locked(session, claimed, worker_id, claimed.run_id)
            else:
                self._require_claimed_cancel_job_locked(session, claimed, worker_id, claimed.run_id)
            run = session.get(RunRecord, claimed.run_id)
            job = session.get(JobRecord, claimed.id)
            if run is None or job is None or job.tenant_id != run.tenant_id:
                raise LeaseLost()
            return run.tenant_id, run.subject_id

    def begin_action_dispatch(
        self,
        action_id: str,
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
        downstream_idempotency_key: str | None = None,
    ) -> bool:
        """Start an Action, or reclaim a local ledger-backed attempt under a new lease."""

        with self._sessions.begin() as session:
            lease_epoch = 0
            if claimed_job is not None:
                if worker_id is None:
                    raise ValueError("worker_id is required with a claimed job")
                job = session.scalar(
                    select(JobRecord).where(JobRecord.id == claimed_job.id).with_for_update()
                )
                now = session.scalar(select(func.now()))
                if now is None:
                    raise RuntimeError("database clock is unavailable")
                if not self._lease_matches(job, claimed_job, worker_id, now=now):
                    raise LeaseLost()
                assert job is not None
                lease_epoch = claimed_job.lease_epoch
            action = session.scalar(
                select(ActionRecord).where(ActionRecord.id == action_id).with_for_update()
            )
            if claimed_job is not None:
                assert job is not None
                if job.kind == "advance_run":
                    if action is None or job.run_id != action.run_id:
                        raise LeaseLost()
                elif job.action_id != action_id:
                    raise LeaseLost()
            if action is None:
                return False
            status = ActionStatus(action.status)
            recovering_local = (
                status == ActionStatus.RUNNING
                and claimed_job is not None
                and job is not None
                and job.kind == "advance_run"
                and action.effect_semantics == "local_transactional"
                and action.capability_id in LEASE_REPLAYABLE_LOCAL_CAPABILITIES
                and action.lease_epoch < lease_epoch
            )
            if status != ActionStatus.READY and not recovering_local:
                return False
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            if RunStatus(run.status) != RunStatus.RUNNING:
                raise AlreadyTerminal()
            if (
                claimed_job is not None
                and action.capability_id in LEASE_REPLAYABLE_LOCAL_CAPABILITIES
                and not downstream_idempotency_key
            ):
                raise InvalidOperation("a leased local effect requires its Action business key")
            if recovering_local:
                if downstream_idempotency_key != action.downstream_idempotency_key:
                    raise VersionConflict()
                previous = session.scalar(
                    select(ActionAttemptRecord)
                    .where(ActionAttemptRecord.action_id == action_id)
                    .order_by(ActionAttemptRecord.attempt_no.desc())
                    .limit(1)
                    .with_for_update()
                )
                if previous is None or previous.status != "started":
                    raise VersionConflict()
                previous.status = "outcome_unknown"
                previous.receipt = {"reason": "lease_reclaimed", "effect": "local_transactional"}
                previous.completed_at = datetime.now(UTC)
            else:
                action.status = ActionStatus.RUNNING.value
            action.version += 1
            action.lease_epoch = lease_epoch
            if downstream_idempotency_key is not None:
                action.downstream_idempotency_key = downstream_idempotency_key
            attempt_no = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(ActionAttemptRecord.attempt_no), 0)).where(
                            ActionAttemptRecord.action_id == action_id
                        )
                    )
                    or 0
                )
                + 1
            )
            session.add(
                ActionAttemptRecord(
                    id=_identifier("attempt"),
                    action_id=action_id,
                    attempt_no=attempt_no,
                    status="started",
                    lease_epoch=lease_epoch,
                    downstream_idempotency_key=downstream_idempotency_key,
                    dispatch_evidence={"dispatch_started": True},
                )
            )
            self._append_event_locked(
                session,
                run,
                "action.retry_started" if recovering_local else "action.started",
                "action_gateway",
                {
                    "action_id": action.id,
                    "capability_id": action.capability_id,
                    "attempt_no": attempt_no,
                    "recovered_local": recovering_local,
                },
                action_id=action.id,
            )
            return True

    def ensure_run_running(
        self,
        run_id: str,
        *,
        actor_ref: str,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> RunSnapshot:
        """Idempotently start an admitted Run through the kernel transaction."""

        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
            if run is None:
                raise ResourceNotFound("run")
            current = RunStatus(run.status)
            if current == RunStatus.QUEUED:
                require_run_transition(current, RunStatus.RUNNING)
                run.status = RunStatus.RUNNING.value
                run.version += 1
                run.event_seq += 1
                run.updated_at = datetime.now(UTC)
                session.add(
                    EventRecord(
                        id=_identifier("evt"),
                        run_id=run.id,
                        seq=run.event_seq,
                        event_type="run.started",
                        actor_ref=actor_ref,
                        data={"status": RunStatus.RUNNING.value, "version": run.version},
                    )
                )
            elif current != RunStatus.RUNNING:
                raise AlreadyTerminal()
            session.flush()
            return self._snapshot(run)

    def finish_implicit_run(
        self,
        run_id: str,
        *,
        succeeded: bool,
        actor_ref: str,
    ) -> RunSnapshot:
        """Finish a direct-HTTP implicit Run and retire its durable wakeup."""

        target = RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED
        with self._sessions.begin() as session:
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
            if run is None:
                raise ResourceNotFound("run")
            current = RunStatus(run.status)
            if current in TERMINAL_RUN_STATUSES:
                return self._snapshot(run)
            require_run_transition(current, target)
            if self._has_nonterminal_children(session, run_id):
                raise VersionConflict()
            run.status = target.value
            run.version += 1
            run.event_seq += 1
            run.updated_at = datetime.now(UTC)
            session.add(
                EventRecord(
                    id=_identifier("evt"),
                    run_id=run.id,
                    seq=run.event_seq,
                    event_type=f"run.{target.value}",
                    actor_ref=actor_ref,
                    data={"status": target.value, "version": run.version},
                )
            )
            session.execute(
                update(JobRecord)
                .where(JobRecord.run_id == run_id, JobRecord.status == "ready")
                .values(status="done")
            )
            session.flush()
            return self._snapshot(run)

    def record_admitted_action(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        turn_id: int,
        call_index: int,
        request_hash: str,
        capability_id: str = "legacy",
        capability_revision: str,
        effect_semantics: str,
        bound_operation: dict[str, Any] | None = None,
    ) -> str:
        """Persist a policy-admitted Action without executing it.

        P02's ActionGateway will be the caller. Keeping this mutation here means
        even the in-process gateway cannot write Action state directly.
        """

        return self.get_or_record_admitted_action(
            context,
            run_id,
            turn_id=turn_id,
            call_index=call_index,
            request_hash=request_hash,
            capability_id=capability_id,
            capability_revision=capability_revision,
            effect_semantics=effect_semantics,
            bound_operation=bound_operation or {},
        ).action.id

    def get_or_record_admitted_action(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        turn_id: int,
        call_index: int,
        request_hash: str,
        capability_id: str,
        capability_revision: str,
        effect_semantics: str,
        bound_operation: dict[str, Any],
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ActionAdmission:
        """Return the immutable action slot or create it exactly once."""

        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = session.scalar(
                select(RunRecord)
                .where(
                    RunRecord.id == run_id,
                    RunRecord.tenant_id == context.tenant_id,
                    RunRecord.subject_id == context.subject_id,
                )
                .with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            existing = session.scalar(
                select(ActionRecord).where(
                    ActionRecord.run_id == run_id,
                    ActionRecord.turn_id == turn_id,
                    ActionRecord.call_index == call_index,
                )
            )
            if existing is not None:
                if (
                    existing.request_hash != request_hash
                    or existing.capability_id != capability_id
                    or existing.capability_revision != capability_revision
                ):
                    raise IdempotencyConflict()
                return ActionAdmission(self._action_snapshot(existing), True)
            if RunStatus(run.status) not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                raise AlreadyTerminal()
            action_id = _identifier("action")
            session.add(
                ActionRecord(
                    id=action_id,
                    run_id=run_id,
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    turn_id=turn_id,
                    call_index=call_index,
                    status=ActionStatus.READY.value,
                    request_hash=request_hash,
                    capability_id=capability_id,
                    capability_revision=capability_revision,
                    effect_semantics=effect_semantics,
                    bound_operation=bound_operation,
                    policy_revision=run.policy_revision,
                    scope_fingerprint=scope_fingerprint(context.scopes),
                    version=0,
                    lease_epoch=0,
                )
            )
            session.flush()
            action = session.get(ActionRecord, action_id)
            if action is None:
                raise RuntimeError("admitted action disappeared")
            self._append_event_locked(
                session,
                run,
                "action.ready",
                "action_gateway",
                {
                    "action_id": action.id,
                    "capability_id": action.capability_id,
                    "turn_id": action.turn_id,
                    "call_index": action.call_index,
                },
                action_id=action.id,
            )
            session.flush()
            return ActionAdmission(self._action_snapshot(action), False)

    def complete_action(
        self,
        action_id: str,
        *,
        result: dict[str, Any],
        succeeded: bool,
        transport_kind: str,
        response_status: int,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ActionSnapshot:
        """Commit an execution result through the kernel state owner."""

        with self._sessions.begin() as session:
            if claimed_job is not None:
                if worker_id is None:
                    raise ValueError("worker_id is required with a claimed job")
                job = session.scalar(
                    select(JobRecord).where(JobRecord.id == claimed_job.id).with_for_update()
                )
                now = session.scalar(select(func.now()))
                if now is None:
                    raise RuntimeError("database clock is unavailable")
                if not self._lease_matches(job, claimed_job, worker_id, now=now):
                    raise LeaseLost()
            action = session.scalar(
                select(ActionRecord).where(ActionRecord.id == action_id).with_for_update()
            )
            if action is None:
                raise ResourceNotFound("action")
            if claimed_job is not None:
                assert job is not None
                if job.kind == "advance_run":
                    if job.run_id != action.run_id:
                        raise LeaseLost()
                elif job.action_id != action_id:
                    raise LeaseLost()
            if ActionStatus(action.status) == ActionStatus.SUCCEEDED:
                return self._action_snapshot(action)
            if ActionStatus(action.status) != ActionStatus.RUNNING:
                raise VersionConflict()
            if claimed_job is not None and action.lease_epoch != claimed_job.lease_epoch:
                raise LeaseLost()
            action.status = ActionStatus.SUCCEEDED.value if succeeded else ActionStatus.FAILED.value
            action.result = result
            action.version += 1
            attempt = session.scalar(
                select(ActionAttemptRecord)
                .where(ActionAttemptRecord.action_id == action_id)
                .order_by(ActionAttemptRecord.attempt_no.desc())
                .limit(1)
                .with_for_update()
            )
            if attempt is None or attempt.status != "started":
                raise VersionConflict()
            attempt.status = "succeeded" if succeeded else "failed"
            attempt.receipt = redact(result)
            attempt.completed_at = datetime.now(UTC)
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            session.add(
                HttpExchangeRecord(
                    id=_identifier("exchange"),
                    action_id=action.id,
                    transport_kind=transport_kind,
                    request_hash=action.request_hash,
                    response_status=response_status,
                )
            )
            self._append_event_locked(
                session,
                run,
                "action.succeeded" if succeeded else "action.failed",
                "action_gateway",
                {
                    "action_id": action.id,
                    "capability_id": action.capability_id,
                    "response_status": response_status,
                },
                action_id=action.id,
            )
            session.flush()
            return self._action_snapshot(action)

    def get_action(self, context: TrustedContext, action_id: str) -> ActionSnapshot:
        with self._sessions() as session:
            action = session.scalar(
                select(ActionRecord).where(
                    ActionRecord.id == action_id,
                    ActionRecord.tenant_id == context.tenant_id,
                    ActionRecord.subject_id == context.subject_id,
                )
            )
            if action is None:
                raise ResourceNotFound("action")
            return self._action_snapshot(action)

    def list_actions(self, context: TrustedContext, run_id: str) -> tuple[ActionSnapshot, ...]:
        self.get_run(context, run_id)
        with self._sessions() as session:
            rows = session.scalars(
                select(ActionRecord)
                .where(ActionRecord.run_id == run_id)
                .order_by(ActionRecord.turn_id, ActionRecord.call_index)
            ).all()
            return tuple(self._action_snapshot(row) for row in rows)

    def request_approval(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        prompt: str,
        target_summary: str,
        expires_at: datetime,
        resource_versions: dict[str, Any] | None = None,
    ) -> InputRequestSnapshot:
        if expires_at <= datetime.now(UTC):
            raise InputRequestExpired()
        with self._sessions.begin() as session:
            action = self._owned_action_locked(session, context, action_id)
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            if action.approval_request_id is not None:
                existing = session.get(InputRequestRecord, action.approval_request_id)
                if existing is not None and existing.status == "pending":
                    return self._input_request_snapshot(existing)
            if ActionStatus(action.status) != ActionStatus.READY:
                raise VersionConflict()
            if RunStatus(run.status) != RunStatus.RUNNING:
                raise AlreadyTerminal()
            request = InputRequestRecord(
                id=_identifier("input"),
                run_id=run.id,
                action_id=action.id,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                kind="approval",
                status="pending",
                version=0,
                prompt=prompt,
                request_hash=action.request_hash,
                requested_schema={},
                target_summary=target_summary,
                capability_revision=action.capability_revision,
                policy_revision=run.policy_revision,
                scope_fingerprint=action.scope_fingerprint,
                resource_versions=resource_versions or {},
                expires_at=expires_at,
            )
            session.add(request)
            action.status = ActionStatus.WAITING_INPUT.value
            action.approval_request_id = request.id
            action.version += 1
            require_run_transition(RunStatus(run.status), RunStatus.WAITING_INPUT)
            run.status = RunStatus.WAITING_INPUT.value
            run.version += 1
            self._append_event_locked(
                session,
                run,
                "input.requested",
                "policy",
                {
                    "input_request_id": request.id,
                    "action_id": action.id,
                    "kind": request.kind,
                    "request_hash": request.request_hash,
                    "target_summary": target_summary,
                },
                action_id=action.id,
            )
            session.flush()
            return self._input_request_snapshot(request)

    def request_clarification(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        prompt: str,
        request_hash: str,
        requested_schema: dict[str, Any] | None = None,
        expires_at: datetime,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> InputRequestSnapshot:
        if expires_at <= datetime.now(UTC):
            raise InputRequestExpired()
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = self._owned_run_locked(session, context, run_id)
            existing = session.scalar(
                select(InputRequestRecord).where(
                    InputRequestRecord.run_id == run_id,
                    InputRequestRecord.action_id.is_(None),
                    InputRequestRecord.kind == "clarification",
                    InputRequestRecord.request_hash == request_hash,
                )
            )
            if existing is not None:
                return self._input_request_snapshot(existing)
            if RunStatus(run.status) != RunStatus.RUNNING:
                raise AlreadyTerminal()
            request = InputRequestRecord(
                id=_identifier("input"),
                run_id=run.id,
                action_id=None,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                kind="clarification",
                status="pending",
                version=0,
                prompt=prompt,
                request_hash=request_hash,
                requested_schema=requested_schema or {"type": "object"},
                target_summary=None,
                capability_revision=None,
                policy_revision=run.policy_revision,
                scope_fingerprint=scope_fingerprint(context.scopes),
                resource_versions={},
                expires_at=expires_at,
            )
            session.add(request)
            require_run_transition(RunStatus(run.status), RunStatus.WAITING_INPUT)
            run.status = RunStatus.WAITING_INPUT.value
            run.version += 1
            self._append_event_locked(
                session,
                run,
                "input.requested",
                "runner",
                {
                    "input_request_id": request.id,
                    "kind": request.kind,
                    "request_hash": request.request_hash,
                    "prompt": prompt,
                },
            )
            session.flush()
            return self._input_request_snapshot(request)

    def get_input_request(
        self,
        context: TrustedContext,
        input_request_id: str,
    ) -> InputRequestSnapshot:
        with self._sessions() as session:
            request = session.scalar(
                select(InputRequestRecord).where(
                    InputRequestRecord.id == input_request_id,
                    InputRequestRecord.tenant_id == context.tenant_id,
                    InputRequestRecord.subject_id == context.subject_id,
                )
            )
            if request is None:
                raise ResourceNotFound("input request")
            return self._input_request_snapshot(request)

    def respond_to_input(
        self,
        context: TrustedContext,
        input_request_id: str,
        *,
        expected_version: int,
        decision: str,
        values: dict[str, Any] | None,
        comment: str | None,
        idempotency_key: str,
    ) -> InputRequestSnapshot:
        if decision not in {"approve", "deny", "submit"}:
            raise ValueError("unsupported input decision")
        expired = False
        snapshot: InputRequestSnapshot | None = None
        with self._sessions.begin() as session:
            replay = session.scalar(
                select(InputResponseRecord).where(
                    InputResponseRecord.tenant_id == context.tenant_id,
                    InputResponseRecord.subject_id == context.subject_id,
                    InputResponseRecord.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if (
                    replay.input_request_id != input_request_id
                    or replay.request_version != expected_version
                    or replay.decision != decision
                    or replay.values != values
                    or replay.comment != comment
                ):
                    raise IdempotencyConflict()
                request = session.get(InputRequestRecord, input_request_id)
                if request is None:
                    raise ResourceNotFound("input request")
                return self._input_request_snapshot(request)
            request = session.scalar(
                select(InputRequestRecord)
                .where(
                    InputRequestRecord.id == input_request_id,
                    InputRequestRecord.tenant_id == context.tenant_id,
                    InputRequestRecord.subject_id == context.subject_id,
                )
                .with_for_update()
            )
            if request is None:
                raise ResourceNotFound("input request")
            if request.version != expected_version or request.status != "pending":
                replay = session.scalar(
                    select(InputResponseRecord).where(
                        InputResponseRecord.tenant_id == context.tenant_id,
                        InputResponseRecord.subject_id == context.subject_id,
                        InputResponseRecord.idempotency_key == idempotency_key,
                    )
                )
                if replay is not None and (
                    replay.input_request_id == input_request_id
                    and replay.request_version == expected_version
                    and replay.decision == decision
                    and replay.values == values
                    and replay.comment == comment
                ):
                    return self._input_request_snapshot(request)
                raise VersionConflict()
            action = (
                None
                if request.action_id is None
                else session.scalar(
                    select(ActionRecord)
                    .where(ActionRecord.id == request.action_id)
                    .with_for_update()
                )
            )
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == request.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            now = datetime.now(UTC)
            if request.expires_at <= now:
                request.status = "expired"
                request.version += 1
                request.decided_at = now
                self._append_event_locked(
                    session,
                    run,
                    "input.expired",
                    "kernel",
                    {"input_request_id": request.id, "action_id": request.action_id},
                    action_id=request.action_id,
                )
                expired = True
                snapshot = self._input_request_snapshot(request)
            else:
                if request.kind == "approval" and decision == "submit":
                    raise VersionConflict()
                if request.kind != "approval" and decision in {"approve", "deny"}:
                    raise VersionConflict()
                if action is not None and (
                    action.request_hash != request.request_hash
                    or action.capability_revision != request.capability_revision
                    or action.policy_revision != request.policy_revision
                ):
                    raise ApprovalRequired("The action changed after the request was created")
                response = InputResponseRecord(
                    id=_identifier("response"),
                    input_request_id=request.id,
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    idempotency_key=idempotency_key,
                    request_version=expected_version,
                    request_hash=request.request_hash,
                    decision=decision,
                    values=values,
                    comment=comment,
                    decided_by=context.subject_id,
                )
                session.add(response)
                request.status = "denied" if decision == "deny" else "answered"
                request.version += 1
                request.decided_at = now
                if action is not None:
                    if decision == "approve" or (
                        request.kind != "approval" and decision == "submit"
                    ):
                        action.status = ActionStatus.READY.value
                    else:
                        action.status = ActionStatus.CANCELLED.value
                    action.version += 1
                if RunStatus(run.status) != RunStatus.WAITING_INPUT:
                    raise VersionConflict()
                target = RunStatus.FAILED if decision == "deny" else RunStatus.QUEUED
                require_run_transition(RunStatus(run.status), target)
                if target == RunStatus.FAILED and self._has_nonterminal_children(session, run.id):
                    raise VersionConflict()
                run.status = target.value
                run.version += 1
                self._append_event_locked(
                    session,
                    run,
                    "input.denied" if decision == "deny" else "input.answered",
                    context.subject_id,
                    {
                        "input_request_id": request.id,
                        "action_id": request.action_id,
                        "decision": decision,
                        "values": values if decision == "submit" else None,
                    },
                    action_id=request.action_id,
                )
                if target == RunStatus.QUEUED:
                    session.add(
                        JobRecord(
                            id=_identifier("job"),
                            tenant_id=context.tenant_id,
                            kind="advance_run",
                            run_id=run.id,
                            action_id=request.action_id,
                            dedupe_key=f"input:{request.id}:response:{request.version}",
                            status="ready",
                            due_at=now,
                        )
                    )
                session.flush()
                snapshot = self._input_request_snapshot(request)
        if expired:
            raise InputRequestExpired()
        if snapshot is None:
            raise RuntimeError("input response transaction produced no snapshot")
        return snapshot

    def validate_approval(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        input_request_id: str | None = None,
        resource_versions: dict[str, Any] | None = None,
    ) -> None:
        with self._sessions() as session:
            action = session.scalar(
                select(ActionRecord).where(
                    ActionRecord.id == action_id,
                    ActionRecord.tenant_id == context.tenant_id,
                    ActionRecord.subject_id == context.subject_id,
                )
            )
            if action is None:
                raise ResourceNotFound("action")
            approval_id = input_request_id or action.approval_request_id
            if approval_id is None or approval_id != action.approval_request_id:
                raise ApprovalRequired()
            request = session.get(InputRequestRecord, approval_id)
            response = session.scalar(
                select(InputResponseRecord).where(
                    InputResponseRecord.input_request_id == approval_id,
                    InputResponseRecord.decision == "approve",
                )
            )
            if (
                request is None
                or response is None
                or request.action_id != action.id
                or request.status != "answered"
                or request.expires_at <= datetime.now(UTC)
                or request.request_hash != action.request_hash
                or request.capability_revision != action.capability_revision
                or request.policy_revision != action.policy_revision
                or request.scope_fingerprint != action.scope_fingerprint
                or action.scope_fingerprint != scope_fingerprint(context.scopes)
                or request.resource_versions != (resource_versions or {})
            ):
                raise ApprovalRequired()

    def validate_dispatch(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        capability_revision: str,
    ) -> None:
        with self._sessions() as session:
            action = session.scalar(
                select(ActionRecord).where(
                    ActionRecord.id == action_id,
                    ActionRecord.tenant_id == context.tenant_id,
                    ActionRecord.subject_id == context.subject_id,
                )
            )
            if action is None:
                raise ResourceNotFound("action")
            run = session.get(RunRecord, action.run_id)
            if run is None:
                raise ResourceNotFound("run")
            if (
                action.capability_revision != capability_revision
                or action.policy_revision != run.policy_revision
                or action.scope_fingerprint != scope_fingerprint(context.scopes)
            ):
                raise PermissionDenied()

    def begin_mcp_rpc(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        integration_id: str,
        profile: str,
        method: str,
        request_payload: dict[str, Any],
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> MCPRPCSnapshot:
        with self._sessions.begin() as session:
            self._require_claimed_job_locked(session, claimed_job, worker_id, action_id)
            action = self._owned_action_locked(session, context, action_id)
            if ActionStatus(action.status) not in {
                ActionStatus.RUNNING,
                ActionStatus.WAITING_EXTERNAL,
            }:
                raise VersionConflict()
            sequence = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(MCPRPCRecord.sequence), 0)).where(
                            MCPRPCRecord.action_id == action_id
                        )
                    )
                    or 0
                )
                + 1
            )
            row = MCPRPCRecord(
                id=_identifier("mcprpc"),
                action_id=action_id,
                integration_id=integration_id,
                profile=profile,
                method=method,
                sequence=sequence,
                correlation_id=_identifier("rpc"),
                request_hash=request_fingerprint(method, integration_id, request_payload),
                status="started",
            )
            session.add(row)
            session.flush()
            return self._mcp_rpc_snapshot(row)

    def complete_mcp_rpc(
        self,
        rpc_id: str,
        *,
        status: str,
        response_summary: dict[str, Any] | None,
    ) -> MCPRPCSnapshot:
        allowed = {"complete", "protocol_error", "input_required", "task", "outcome_unknown"}
        if status not in allowed:
            raise ValueError("unsupported MCP RPC status")
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MCPRPCRecord).where(MCPRPCRecord.id == rpc_id).with_for_update()
            )
            if row is None:
                raise ResourceNotFound("MCP RPC")
            if row.status != "started":
                return self._mcp_rpc_snapshot(row)
            row.status = status
            row.response_summary = redact(response_summary)
            row.completed_at = datetime.now(UTC)
            session.flush()
            return self._mcp_rpc_snapshot(row)

    def record_mcp_input_required(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        rpc_id: str,
        integration_id: str,
        profile: str,
        tool_name: str,
        request_state: str | None,
        input_requests: dict[str, Any],
        prompt: str,
        requested_schema: dict[str, Any],
        expires_at: datetime,
    ) -> InputRequestSnapshot:
        if expires_at <= datetime.now(UTC):
            raise InputRequestExpired()
        with self._sessions.begin() as session:
            action = self._owned_action_locked(session, context, action_id)
            if ActionStatus(action.status) != ActionStatus.RUNNING:
                raise VersionConflict()
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            rpc = session.scalar(
                select(MCPRPCRecord).where(MCPRPCRecord.id == rpc_id).with_for_update()
            )
            if run is None or rpc is None or rpc.action_id != action_id or rpc.status != "started":
                raise VersionConflict()
            sequence = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(MCPContinuationRecord.sequence), 0)).where(
                            MCPContinuationRecord.action_id == action_id
                        )
                    )
                    or 0
                )
                + 1
            )
            request = InputRequestRecord(
                id=_identifier("input"),
                run_id=run.id,
                action_id=action.id,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                kind="external_input",
                status="pending",
                version=0,
                prompt=prompt,
                request_hash=action.request_hash,
                requested_schema=requested_schema,
                target_summary=f"MCP {integration_id}/{tool_name} continuation",
                capability_revision=action.capability_revision,
                policy_revision=action.policy_revision,
                scope_fingerprint=action.scope_fingerprint,
                resource_versions={},
                expires_at=expires_at,
            )
            session.add(request)
            session.flush()
            session.add(
                MCPContinuationRecord(
                    id=_identifier("mcpcont"),
                    action_id=action.id,
                    input_request_id=request.id,
                    integration_id=integration_id,
                    profile=profile,
                    tool_name=tool_name,
                    sequence=sequence,
                    request_state=request_state,
                    input_requests=input_requests,
                    status="pending",
                )
            )
            attempt = self._latest_attempt_locked(session, action.id)
            if attempt.status != "started":
                raise VersionConflict()
            attempt.status = "succeeded"
            attempt.receipt = {"result_type": "input_required"}
            attempt.completed_at = datetime.now(UTC)
            rpc.status = "input_required"
            rpc.response_summary = {
                "result_type": "input_required",
                "request_count": len(input_requests),
            }
            rpc.completed_at = datetime.now(UTC)
            action.status = ActionStatus.WAITING_INPUT.value
            action.version += 1
            require_run_transition(RunStatus(run.status), RunStatus.WAITING_INPUT)
            run.status = RunStatus.WAITING_INPUT.value
            run.version += 1
            self._append_event_locked(
                session,
                run,
                "mcp.input_required",
                "mcp_adapter",
                {
                    "action_id": action.id,
                    "input_request_id": request.id,
                    "integration_id": integration_id,
                    "profile": profile,
                    "continuation_sequence": sequence,
                },
                action_id=action.id,
            )
            session.flush()
            return self._input_request_snapshot(request)

    def get_mcp_continuation(
        self,
        context: TrustedContext,
        action_id: str,
    ) -> MCPContinuationSnapshot:
        with self._sessions() as session:
            self._owned_action_locked(session, context, action_id)
            row = session.scalar(
                select(MCPContinuationRecord)
                .where(MCPContinuationRecord.action_id == action_id)
                .order_by(MCPContinuationRecord.sequence.desc())
                .limit(1)
            )
            if row is None:
                raise ResourceNotFound("MCP continuation")
            response = session.scalar(
                select(InputResponseRecord).where(
                    InputResponseRecord.input_request_id == row.input_request_id
                )
            )
            return self._mcp_continuation_snapshot(row, response)

    def mark_mcp_continuation_resumed(self, continuation_id: str) -> None:
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MCPContinuationRecord)
                .where(MCPContinuationRecord.id == continuation_id)
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFound("MCP continuation")
            if row.status == "resumed":
                return
            response = session.scalar(
                select(InputResponseRecord).where(
                    InputResponseRecord.input_request_id == row.input_request_id
                )
            )
            if response is None or response.decision != "submit":
                raise VersionConflict()
            row.status = "resumed"
            row.resumed_at = datetime.now(UTC)

    def record_mcp_task(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        rpc_id: str,
        integration_id: str,
        profile: str,
        task: dict[str, Any],
    ) -> MCPTaskSnapshot:
        with self._sessions.begin() as session:
            action = self._owned_action_locked(session, context, action_id)
            if ActionStatus(action.status) != ActionStatus.RUNNING:
                raise VersionConflict()
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            rpc = session.scalar(
                select(MCPRPCRecord).where(MCPRPCRecord.id == rpc_id).with_for_update()
            )
            if run is None or rpc is None or rpc.action_id != action_id or rpc.status != "started":
                raise VersionConflict()
            existing = session.scalar(
                select(MCPTaskRecord).where(MCPTaskRecord.action_id == action_id)
            )
            if existing is not None:
                return self._mcp_task_snapshot(existing)
            now = datetime.now(UTC)
            poll_interval_ms = task.get("poll_interval_ms")
            row = MCPTaskRecord(
                id=_identifier("mcptask"),
                action_id=action.id,
                integration_id=integration_id,
                profile=profile,
                task_id=str(task["task_id"]),
                status=str(task["status"]),
                status_message=task.get("status_message"),
                version=0,
                created_at_remote=str(task["created_at"]),
                last_updated_at_remote=str(task["last_updated_at"]),
                ttl_ms=task.get("ttl_ms"),
                poll_interval_ms=poll_interval_ms,
                input_requests=task.get("input_requests") or {},
                result=task.get("result"),
                error=task.get("error"),
                next_poll_at=(
                    now + timedelta(milliseconds=int(poll_interval_ms or 0))
                    if task["status"] in {"working", "input_required"}
                    else None
                ),
            )
            session.add(row)
            attempt = self._latest_attempt_locked(session, action.id)
            if attempt.status != "started":
                raise VersionConflict()
            attempt.status = "succeeded"
            attempt.receipt = {"result_type": "task", "task_id": row.task_id}
            attempt.upstream_handle = row.task_id
            attempt.completed_at = now
            rpc.status = "task"
            rpc.response_summary = {
                "result_type": "task",
                "task_id": row.task_id,
                "status": row.status,
            }
            rpc.completed_at = now
            action.status = ActionStatus.WAITING_EXTERNAL.value
            action.upstream_handle = row.task_id
            action.version += 1
            if row.status in {"working", "input_required"}:
                session.add(
                    JobRecord(
                        id=_identifier("job"),
                        tenant_id=context.tenant_id,
                        kind="mcp_task_poll",
                        run_id=run.id,
                        action_id=action.id,
                        dedupe_key=f"mcp-task:{row.id}:poll:{row.version}",
                        status="ready",
                        due_at=row.next_poll_at or now,
                    )
                )
            self._append_event_locked(
                session,
                run,
                "mcp.task_created",
                "mcp_adapter",
                {
                    "action_id": action.id,
                    "integration_id": integration_id,
                    "profile": profile,
                    "task_id": row.task_id,
                    "status": row.status,
                },
                action_id=action.id,
            )
            session.flush()
            return self._mcp_task_snapshot(row)

    def get_mcp_task(self, context: TrustedContext, action_id: str) -> MCPTaskSnapshot:
        with self._sessions() as session:
            self._owned_action_locked(session, context, action_id)
            row = session.scalar(select(MCPTaskRecord).where(MCPTaskRecord.action_id == action_id))
            if row is None:
                raise ResourceNotFound("MCP task")
            return self._mcp_task_snapshot(row)

    def get_latest_external_input_response(
        self,
        context: TrustedContext,
        action_id: str,
    ) -> tuple[InputRequestSnapshot, dict[str, Any]]:
        with self._sessions() as session:
            self._owned_action_locked(session, context, action_id)
            request = session.scalar(
                select(InputRequestRecord)
                .where(
                    InputRequestRecord.action_id == action_id,
                    InputRequestRecord.kind == "external_input",
                )
                .order_by(InputRequestRecord.created_at.desc())
                .limit(1)
            )
            if request is None:
                raise ResourceNotFound("external input request")
            response = session.scalar(
                select(InputResponseRecord).where(
                    InputResponseRecord.input_request_id == request.id,
                    InputResponseRecord.decision == "submit",
                )
            )
            if response is None or response.values is None:
                raise VersionConflict()
            return self._input_request_snapshot(request), dict(response.values)

    def get_latest_external_input_request(
        self,
        context: TrustedContext,
        action_id: str,
    ) -> InputRequestSnapshot:
        with self._sessions() as session:
            self._owned_action_locked(session, context, action_id)
            request = session.scalar(
                select(InputRequestRecord)
                .where(
                    InputRequestRecord.action_id == action_id,
                    InputRequestRecord.kind == "external_input",
                )
                .order_by(InputRequestRecord.created_at.desc())
                .limit(1)
            )
            if request is None:
                raise ResourceNotFound("external input request")
            return self._input_request_snapshot(request)

    def record_mcp_task_observation(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        rpc_id: str,
        task: dict[str, Any],
        prompt: str | None = None,
        requested_schema: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> tuple[MCPTaskSnapshot, InputRequestSnapshot | None]:
        with self._sessions.begin() as session:
            self._require_claimed_job_locked(session, claimed_job, worker_id, action_id)
            action = self._owned_action_locked(session, context, action_id)
            row = session.scalar(
                select(MCPTaskRecord).where(MCPTaskRecord.action_id == action_id).with_for_update()
            )
            rpc = session.scalar(
                select(MCPRPCRecord).where(MCPRPCRecord.id == rpc_id).with_for_update()
            )
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if row is None or rpc is None or run is None or rpc.status != "started":
                raise VersionConflict()
            if str(task["task_id"]) != row.task_id:
                raise VersionConflict()
            row.status = str(task["status"])
            row.status_message = task.get("status_message")
            row.last_updated_at_remote = str(task["last_updated_at"])
            row.ttl_ms = task.get("ttl_ms")
            row.poll_interval_ms = task.get("poll_interval_ms")
            row.input_requests = task.get("input_requests") or {}
            row.result = task.get("result")
            row.error = task.get("error")
            row.version += 1
            now = datetime.now(UTC)
            rpc.status = "complete"
            rpc.response_summary = {"task_id": row.task_id, "status": row.status}
            rpc.completed_at = now
            input_snapshot: InputRequestSnapshot | None = None
            if row.status == "working":
                delay = int(row.poll_interval_ms or 0)
                row.next_poll_at = now + timedelta(milliseconds=delay)
                session.add(
                    JobRecord(
                        id=_identifier("job"),
                        tenant_id=context.tenant_id,
                        kind="mcp_task_poll",
                        run_id=run.id,
                        action_id=action.id,
                        dedupe_key=f"mcp-task:{row.id}:poll:{row.version}",
                        status="ready",
                        due_at=row.next_poll_at,
                    )
                )
            elif row.status == "input_required":
                if prompt is None or requested_schema is None or expires_at is None:
                    raise ValueError("task input_required needs a durable input request")
                if expires_at <= now:
                    raise InputRequestExpired()
                existing = session.scalar(
                    select(InputRequestRecord).where(
                        InputRequestRecord.action_id == action.id,
                        InputRequestRecord.kind == "external_input",
                        InputRequestRecord.status == "pending",
                    )
                )
                if existing is None:
                    existing = InputRequestRecord(
                        id=_identifier("input"),
                        run_id=run.id,
                        action_id=action.id,
                        tenant_id=context.tenant_id,
                        subject_id=context.subject_id,
                        kind="external_input",
                        status="pending",
                        version=0,
                        prompt=prompt,
                        request_hash=action.request_hash,
                        requested_schema=requested_schema,
                        target_summary=f"MCP task {row.task_id} input",
                        capability_revision=action.capability_revision,
                        policy_revision=action.policy_revision,
                        scope_fingerprint=action.scope_fingerprint,
                        resource_versions={},
                        expires_at=expires_at,
                    )
                    session.add(existing)
                    action.status = ActionStatus.WAITING_INPUT.value
                    action.version += 1
                    require_run_transition(RunStatus(run.status), RunStatus.WAITING_INPUT)
                    run.status = RunStatus.WAITING_INPUT.value
                    run.version += 1
                    self._append_event_locked(
                        session,
                        run,
                        "mcp.task_input_required",
                        "mcp_adapter",
                        {
                            "action_id": action.id,
                            "task_id": row.task_id,
                            "input_request_id": existing.id,
                        },
                        action_id=action.id,
                    )
                    session.flush()
                input_snapshot = self._input_request_snapshot(existing)
            else:
                row.next_poll_at = None
            self._append_event_locked(
                session,
                run,
                "mcp.task_observed",
                "mcp_adapter",
                {"action_id": action.id, "task_id": row.task_id, "status": row.status},
                action_id=action.id,
            )
            session.flush()
            return self._mcp_task_snapshot(row), input_snapshot

    def mark_mcp_task_update_acknowledged(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        rpc_id: str,
    ) -> MCPTaskSnapshot:
        with self._sessions.begin() as session:
            action = self._owned_action_locked(session, context, action_id)
            row = session.scalar(
                select(MCPTaskRecord).where(MCPTaskRecord.action_id == action_id).with_for_update()
            )
            rpc = session.scalar(
                select(MCPRPCRecord).where(MCPRPCRecord.id == rpc_id).with_for_update()
            )
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if (
                row is None
                or rpc is None
                or run is None
                or rpc.status != "started"
                or ActionStatus(action.status) != ActionStatus.RUNNING
            ):
                raise VersionConflict()
            now = datetime.now(UTC)
            attempt = self._latest_attempt_locked(session, action.id)
            attempt.status = "succeeded"
            attempt.receipt = {"method": "tasks/update", "task_id": row.task_id}
            attempt.completed_at = now
            rpc.status = "complete"
            rpc.response_summary = {"acknowledged": True, "task_id": row.task_id}
            rpc.completed_at = now
            action.status = ActionStatus.WAITING_EXTERNAL.value
            action.version += 1
            row.status = "working"
            row.input_requests = {}
            row.version += 1
            row.next_poll_at = now + timedelta(milliseconds=int(row.poll_interval_ms or 0))
            session.add(
                JobRecord(
                    id=_identifier("job"),
                    tenant_id=context.tenant_id,
                    kind="mcp_task_poll",
                    run_id=run.id,
                    action_id=action.id,
                    dedupe_key=f"mcp-task:{row.id}:poll:{row.version}",
                    status="ready",
                    due_at=row.next_poll_at,
                )
            )
            self._append_event_locked(
                session,
                run,
                "mcp.task_input_submitted",
                "mcp_adapter",
                {"action_id": action.id, "task_id": row.task_id},
                action_id=action.id,
            )
            session.flush()
            return self._mcp_task_snapshot(row)

    def mark_mcp_task_cancel_acknowledged(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        rpc_id: str,
    ) -> MCPTaskSnapshot:
        with self._sessions.begin() as session:
            action = self._owned_action_locked(session, context, action_id)
            row = session.scalar(
                select(MCPTaskRecord).where(MCPTaskRecord.action_id == action_id).with_for_update()
            )
            rpc = session.scalar(
                select(MCPRPCRecord).where(MCPRPCRecord.id == rpc_id).with_for_update()
            )
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if row is None or rpc is None or run is None or rpc.status != "started":
                raise VersionConflict()
            now = datetime.now(UTC)
            rpc.status = "complete"
            rpc.response_summary = {"acknowledged": True, "task_id": row.task_id}
            rpc.completed_at = now
            row.cancel_acknowledged = True
            row.version += 1
            action.cancel_receipt = {
                "method": "tasks/cancel",
                "acknowledged": True,
                "stopped": False,
                "task_id": row.task_id,
            }
            if ActionStatus(action.status) != ActionStatus.WAITING_EXTERNAL:
                action.status = ActionStatus.WAITING_EXTERNAL.value
                action.version += 1
            self._append_event_locked(
                session,
                run,
                "mcp.task_cancel_acknowledged",
                "mcp_adapter",
                {"action_id": action.id, "task_id": row.task_id, "stopped": False},
                action_id=action.id,
            )
            session.flush()
            return self._mcp_task_snapshot(row)

    def finalize_mcp_task_cancelled(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ActionSnapshot:
        with self._sessions.begin() as session:
            self._require_claimed_job_locked(session, claimed_job, worker_id, action_id)
            action = self._owned_action_locked(session, context, action_id)
            task = session.scalar(select(MCPTaskRecord).where(MCPTaskRecord.action_id == action_id))
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if task is None or run is None or task.status != "cancelled":
                raise VersionConflict()
            if ActionStatus(action.status) == ActionStatus.CANCELLED:
                return self._action_snapshot(action)
            if ActionStatus(action.status) != ActionStatus.WAITING_EXTERNAL:
                raise VersionConflict()
            action.status = ActionStatus.CANCELLED.value
            action.result = {
                "status_code": 200,
                "value": {"result_type": "task", "task_id": task.task_id, "status": "cancelled"},
            }
            action.version += 1
            self._append_event_locked(
                session,
                run,
                "action.cancelled",
                "mcp_adapter",
                {"action_id": action.id, "task_id": task.task_id},
                action_id=action.id,
            )
            if (
                RunStatus(run.status) == RunStatus.CANCELLING
                and not self._has_unresolved_actions(session, run.id)
                and not self._has_nonterminal_children(session, run.id)
            ):
                require_run_transition(RunStatus.CANCELLING, RunStatus.CANCELLED)
                run.status = RunStatus.CANCELLED.value
                run.version += 1
                self._append_event_locked(
                    session,
                    run,
                    "run.cancelled",
                    "mcp_adapter",
                    {"status": RunStatus.CANCELLED.value, "version": run.version},
                )
            session.flush()
            return self._action_snapshot(action)

    def begin_mcp_task_completion(
        self,
        action_id: str,
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> bool:
        with self._sessions.begin() as session:
            self._require_claimed_job_locked(session, claimed_job, worker_id, action_id)
            action = session.scalar(
                select(ActionRecord).where(ActionRecord.id == action_id).with_for_update()
            )
            if action is None:
                raise ResourceNotFound("action")
            if ActionStatus(action.status) != ActionStatus.WAITING_EXTERNAL:
                return False
            attempt_no = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(ActionAttemptRecord.attempt_no), 0)).where(
                            ActionAttemptRecord.action_id == action_id
                        )
                    )
                    or 0
                )
                + 1
            )
            action.status = ActionStatus.RUNNING.value
            action.version += 1
            if claimed_job is not None:
                action.lease_epoch = claimed_job.lease_epoch
            session.add(
                ActionAttemptRecord(
                    id=_identifier("attempt"),
                    action_id=action_id,
                    attempt_no=attempt_no,
                    status="started",
                    dispatch_evidence={"local_task_completion": True},
                )
            )
            return True

    def mark_action_outcome_unknown(
        self,
        action_id: str,
        *,
        receipt: dict[str, Any],
        upstream_handle: str | None = None,
    ) -> ActionSnapshot:
        with self._sessions.begin() as session:
            action = session.scalar(
                select(ActionRecord).where(ActionRecord.id == action_id).with_for_update()
            )
            if action is None:
                raise ResourceNotFound("action")
            if ActionStatus(action.status) != ActionStatus.RUNNING:
                raise VersionConflict()
            attempt = self._latest_attempt_locked(session, action.id)
            attempt.status = "outcome_unknown"
            attempt.receipt = redact(receipt)
            attempt.completed_at = datetime.now(UTC)
            action.status = ActionStatus.OUTCOME_UNKNOWN.value
            action.result = receipt
            action.upstream_handle = upstream_handle
            action.version += 1
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            current = RunStatus(run.status)
            if current != RunStatus.BLOCKED:
                require_run_transition(current, RunStatus.BLOCKED)
                run.status = RunStatus.BLOCKED.value
                run.version += 1
            self._append_event_locked(
                session,
                run,
                "action.outcome_unknown",
                "recovery",
                {
                    "action_id": action.id,
                    "attempt_no": attempt.attempt_no,
                    "effect_semantics": action.effect_semantics,
                    "upstream_handle": upstream_handle,
                },
                action_id=action.id,
            )
            session.flush()
            return self._action_snapshot(action)

    def mark_action_retry_wait(
        self,
        action_id: str,
        *,
        receipt: dict[str, Any],
        downstream_idempotency_key: str,
    ) -> ActionSnapshot:
        with self._sessions.begin() as session:
            action = session.scalar(
                select(ActionRecord).where(ActionRecord.id == action_id).with_for_update()
            )
            if action is None:
                raise ResourceNotFound("action")
            if (
                ActionStatus(action.status) != ActionStatus.RUNNING
                or action.effect_semantics != "remote_idempotent"
            ):
                raise VersionConflict()
            if (
                action.downstream_idempotency_key is not None
                and action.downstream_idempotency_key != downstream_idempotency_key
            ):
                raise IdempotencyConflict()
            attempt = self._latest_attempt_locked(session, action.id)
            attempt.status = "outcome_unknown"
            attempt.receipt = redact(receipt)
            attempt.completed_at = datetime.now(UTC)
            action.status = ActionStatus.RETRY_WAIT.value
            action.result = receipt
            action.downstream_idempotency_key = downstream_idempotency_key
            action.version += 1
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            self._append_event_locked(
                session,
                run,
                "action.retry_scheduled",
                "recovery",
                {
                    "action_id": action.id,
                    "attempt_no": attempt.attempt_no,
                    "downstream_idempotency_key": downstream_idempotency_key,
                },
                action_id=action.id,
            )
            session.flush()
            return self._action_snapshot(action)

    def begin_retry_dispatch(
        self,
        action_id: str,
        *,
        downstream_idempotency_key: str,
    ) -> int:
        with self._sessions.begin() as session:
            action = session.scalar(
                select(ActionRecord).where(ActionRecord.id == action_id).with_for_update()
            )
            if action is None:
                raise ResourceNotFound("action")
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            if (
                ActionStatus(action.status) != ActionStatus.RETRY_WAIT
                or action.effect_semantics != "remote_idempotent"
                or action.downstream_idempotency_key != downstream_idempotency_key
                or RunStatus(run.status) != RunStatus.RUNNING
            ):
                raise VersionConflict()
            prior = self._latest_attempt_locked(
                session,
                action.id,
                required_status="outcome_unknown",
            )
            attempt_no = prior.attempt_no + 1
            session.add(
                ActionAttemptRecord(
                    id=_identifier("attempt"),
                    action_id=action.id,
                    attempt_no=attempt_no,
                    status="started",
                    lease_epoch=action.lease_epoch,
                    downstream_idempotency_key=downstream_idempotency_key,
                    dispatch_evidence={"dispatch_started": True, "retry": True},
                )
            )
            action.status = ActionStatus.RUNNING.value
            action.version += 1
            self._append_event_locked(
                session,
                run,
                "action.retry_started",
                "recovery",
                {"action_id": action.id, "attempt_no": attempt_no},
                action_id=action.id,
            )
            return attempt_no

    def record_action_cancel_receipt(
        self,
        action_id: str,
        *,
        receipt: dict[str, Any],
        stopped: bool,
    ) -> ActionSnapshot:
        with self._sessions.begin() as session:
            action = session.scalar(
                select(ActionRecord).where(ActionRecord.id == action_id).with_for_update()
            )
            if action is None:
                raise ResourceNotFound("action")
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            if RunStatus(run.status) != RunStatus.CANCELLING:
                raise VersionConflict()
            action.cancel_receipt = redact(receipt)
            action.status = (
                ActionStatus.CANCELLED.value if stopped else ActionStatus.WAITING_EXTERNAL.value
            )
            action.version += 1
            self._append_event_locked(
                session,
                run,
                "action.cancelled" if stopped else "action.cancel_acknowledged",
                "recovery",
                {"action_id": action.id, "stopped": stopped, "receipt": receipt},
                action_id=action.id,
            )
            if (
                stopped
                and not self._has_unresolved_actions(session, run.id)
                and not self._has_nonterminal_children(session, run.id)
            ):
                require_run_transition(RunStatus(run.status), RunStatus.CANCELLED)
                run.status = RunStatus.CANCELLED.value
                run.version += 1
                self._append_event_locked(
                    session,
                    run,
                    "run.cancelled",
                    "recovery",
                    {"status": RunStatus.CANCELLED.value},
                )
            session.flush()
            return self._action_snapshot(action)

    def reconcile_action(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        expected_version: int,
        resolution: str,
        evidence_ids: list[str],
        comment: str | None,
        idempotency_key: str,
    ) -> ActionSnapshot:
        if (
            resolution
            not in {
                "confirmed_applied",
                "confirmed_not_applied",
                "confirmed_stopped",
            }
            or not evidence_ids
        ):
            raise ValueError("invalid reconciliation evidence")
        with self._sessions.begin() as session:
            action = self._owned_action_locked(session, context, action_id)
            existing = session.scalar(
                select(ReconciliationRecord).where(
                    ReconciliationRecord.action_id == action_id,
                    ReconciliationRecord.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.action_version != expected_version
                    or existing.resolution != resolution
                    or existing.evidence_ids != evidence_ids
                    or existing.comment != comment
                ):
                    raise IdempotencyConflict()
                return self._action_snapshot(action)
            if action.version != expected_version or ActionStatus(action.status) not in {
                ActionStatus.OUTCOME_UNKNOWN,
                ActionStatus.WAITING_EXTERNAL,
            }:
                raise VersionConflict()
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == action.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            session.add(
                ReconciliationRecord(
                    id=_identifier("reconciliation"),
                    action_id=action.id,
                    run_id=run.id,
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    idempotency_key=idempotency_key,
                    action_version=expected_version,
                    resolution=resolution,
                    evidence_ids=evidence_ids,
                    comment=comment,
                    decided_by=context.subject_id,
                )
            )
            current = RunStatus(run.status)
            if resolution == "confirmed_applied":
                action.status = ActionStatus.SUCCEEDED.value
                action.result = {
                    "reconciliation": resolution,
                    "evidence_ids": evidence_ids,
                }
            elif resolution == "confirmed_not_applied":
                action.status = (
                    ActionStatus.CANCELLED.value
                    if current == RunStatus.CANCELLING
                    else ActionStatus.READY.value
                )
                action.result = {
                    "reconciliation": resolution,
                    "evidence_ids": evidence_ids,
                }
            else:
                action.status = ActionStatus.CANCELLED.value
                action.result = {
                    "reconciliation": resolution,
                    "evidence_ids": evidence_ids,
                }
            action.version += 1
            if current == RunStatus.BLOCKED:
                require_run_transition(current, RunStatus.QUEUED)
                run.status = RunStatus.QUEUED.value
                session.add(
                    JobRecord(
                        id=_identifier("job"),
                        tenant_id=context.tenant_id,
                        kind="advance_run",
                        run_id=run.id,
                        action_id=action.id,
                        dedupe_key=f"reconciliation:{action.id}:{action.version}",
                        status="ready",
                        due_at=datetime.now(UTC),
                    )
                )
            run.version += 1
            self._append_event_locked(
                session,
                run,
                "action.reconciled",
                context.subject_id,
                {
                    "action_id": action.id,
                    "resolution": resolution,
                    "evidence_ids": evidence_ids,
                    "comment": comment,
                },
                action_id=action.id,
            )
            if (
                current == RunStatus.CANCELLING
                and not self._has_unresolved_actions(session, run.id)
                and not self._has_nonterminal_children(session, run.id)
            ):
                require_run_transition(current, RunStatus.CANCELLED)
                run.status = RunStatus.CANCELLED.value
                self._append_event_locked(
                    session,
                    run,
                    "run.cancelled",
                    "recovery",
                    {"status": RunStatus.CANCELLED.value, "version": run.version},
                )
            session.flush()
            return self._action_snapshot(action)

    def save_checkpoint(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        checkpoint_seq: int,
        schema_version: str,
        capability_id: str | None,
        capability_revision: str | None,
        runner_cursor: dict[str, Any],
        content: dict[str, Any],
    ) -> CheckpointSnapshot:
        with self._sessions.begin() as session:
            run = self._owned_run_locked(session, context, run_id)
            existing = session.scalar(
                select(CheckpointRecord).where(
                    CheckpointRecord.run_id == run_id,
                    CheckpointRecord.checkpoint_seq == checkpoint_seq,
                )
            )
            if existing is not None:
                if (
                    existing.schema_version != schema_version
                    or existing.capability_id != capability_id
                    or existing.capability_revision != capability_revision
                    or existing.runner_cursor != runner_cursor
                    or existing.content != content
                ):
                    raise IdempotencyConflict()
                return self._checkpoint_snapshot(existing)
            row = CheckpointRecord(
                id=_identifier("checkpoint"),
                run_id=run_id,
                checkpoint_seq=checkpoint_seq,
                schema_version=schema_version,
                capability_id=capability_id,
                capability_revision=capability_revision,
                policy_revision=run.policy_revision,
                runner_cursor=runner_cursor,
                content=content,
            )
            session.add(row)
            session.flush()
            return self._checkpoint_snapshot(row)

    def recover_latest_checkpoint(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        supported_schema_version: str,
        capability_revisions: dict[str, str],
        policy_revision: str,
    ) -> CheckpointSnapshot:
        migration_detail: str | None = None
        snapshot: CheckpointSnapshot | None = None
        with self._sessions.begin() as session:
            run = self._owned_run_locked(session, context, run_id)
            row = session.scalar(
                select(CheckpointRecord)
                .where(CheckpointRecord.run_id == run_id)
                .order_by(CheckpointRecord.checkpoint_seq.desc())
                .limit(1)
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFound("checkpoint")
            problems: list[str] = []
            if row.schema_version != supported_schema_version:
                problems.append(f"checkpoint schema {row.schema_version}")
            if row.policy_revision != policy_revision:
                problems.append(f"policy revision {row.policy_revision}")
            if row.capability_id is not None and capability_revisions.get(row.capability_id) != (
                row.capability_revision
            ):
                problems.append(
                    f"capability {row.capability_id}@{row.capability_revision or 'missing'}"
                )
            if problems:
                migration_detail = "Incompatible " + ", ".join(problems)
                current = RunStatus(run.status)
                if current != RunStatus.BLOCKED:
                    require_run_transition(current, RunStatus.BLOCKED)
                    run.status = RunStatus.BLOCKED.value
                    run.version += 1
                run.failure = {"code": "migration_required", "detail": migration_detail}
                self._append_event_locked(
                    session,
                    run,
                    "run.migration_required",
                    "recovery",
                    {
                        "checkpoint_id": row.id,
                        "schema_version": row.schema_version,
                        "capability_id": row.capability_id,
                        "capability_revision": row.capability_revision,
                        "policy_revision": row.policy_revision,
                    },
                )
            snapshot = self._checkpoint_snapshot(row)
        if migration_detail is not None:
            raise MigrationRequired(migration_detail)
        if snapshot is None:
            raise RuntimeError("checkpoint recovery produced no snapshot")
        return snapshot

    def begin_model_call(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        turn_id: int,
        provider: str,
        model_revision: str,
        request_payload: dict[str, Any],
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ModelCallWork:
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = self._owned_run_locked(session, context, run_id)
            if RunStatus(run.status) != RunStatus.RUNNING:
                raise AlreadyTerminal()
            call = session.scalar(
                select(ModelCallRecord)
                .where(ModelCallRecord.run_id == run_id, ModelCallRecord.turn_id == turn_id)
                .with_for_update()
            )
            if call is not None and call.status != "started":
                return ModelCallWork(self._model_call_snapshot(call), None, None, True)
            if call is None:
                call = ModelCallRecord(
                    id=_identifier("modelcall"),
                    run_id=run_id,
                    turn_id=turn_id,
                    provider=provider,
                    model_revision=model_revision,
                    status="started",
                    request_payload=request_payload,
                    usage={},
                )
                session.add(call)
                session.flush()
                attempt_no = 1
            else:
                if call.provider != provider or call.model_revision != model_revision:
                    raise IdempotencyConflict()
                attempt_no = (
                    int(
                        session.scalar(
                            select(func.count())
                            .select_from(ModelCallAttemptRecord)
                            .where(ModelCallAttemptRecord.model_call_id == call.id)
                        )
                        or 0
                    )
                    + 1
                )
            attempt = ModelCallAttemptRecord(
                id=_identifier("modelattempt"),
                model_call_id=call.id,
                attempt_no=attempt_no,
                status="started",
                usage={},
            )
            session.add(attempt)
            self._append_event_locked(
                session,
                run,
                "model.call_started",
                "runner",
                {"model_call_id": call.id, "turn_id": turn_id, "attempt_no": attempt_no},
            )
            session.flush()
            return ModelCallWork(self._model_call_snapshot(call), attempt.id, attempt_no, False)

    def complete_model_call(
        self,
        model_call_id: str,
        attempt_id: str,
        *,
        response_payload: dict[str, Any],
        parsed_output: Any,
        provider_request_id: str | None,
        usage: dict[str, Any],
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ModelCallSnapshot:
        encoded = json.dumps(
            parsed_output,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with self._sessions.begin() as session:
            if claimed_job is not None:
                run_id = session.scalar(
                    select(ModelCallRecord.run_id).where(ModelCallRecord.id == model_call_id)
                )
                if run_id is None:
                    raise ResourceNotFound("model call")
                self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            call = session.scalar(
                select(ModelCallRecord).where(ModelCallRecord.id == model_call_id).with_for_update()
            )
            if call is None:
                raise ResourceNotFound("model call")
            if call.status != "started":
                return self._model_call_snapshot(call)
            attempt = session.scalar(
                select(ModelCallAttemptRecord)
                .where(
                    ModelCallAttemptRecord.id == attempt_id,
                    ModelCallAttemptRecord.model_call_id == call.id,
                )
                .with_for_update()
            )
            if attempt is None:
                raise ResourceNotFound("model call attempt")
            call.status = "response_committed"
            call.response_payload = response_payload
            call.parsed_output = parsed_output
            call.response_hash = hashlib.sha256(encoded).hexdigest()
            call.provider_request_id = provider_request_id
            call.usage = usage
            call.updated_at = datetime.now(UTC)
            attempt.status = "completed"
            attempt.provider_request_id = provider_request_id
            attempt.usage = usage
            attempt.completed_at = datetime.now(UTC)
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == call.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            self._append_event_locked(
                session,
                run,
                "model.response_committed",
                "runner",
                {
                    "model_call_id": call.id,
                    "turn_id": call.turn_id,
                    "response_hash": call.response_hash,
                },
            )
            session.flush()
            return self._model_call_snapshot(call)

    def fail_model_call_attempt(
        self,
        model_call_id: str,
        attempt_id: str,
        error: dict[str, Any],
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            if claimed_job is not None:
                run_id = session.scalar(
                    select(ModelCallRecord.run_id).where(ModelCallRecord.id == model_call_id)
                )
                if run_id is None:
                    raise ResourceNotFound("model call attempt")
                self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            call = session.scalar(
                select(ModelCallRecord).where(ModelCallRecord.id == model_call_id).with_for_update()
            )
            attempt = session.scalar(
                select(ModelCallAttemptRecord)
                .where(ModelCallAttemptRecord.id == attempt_id)
                .with_for_update()
            )
            if call is None or attempt is None:
                raise ResourceNotFound("model call attempt")
            attempt.status = "failed"
            attempt.error = error
            attempt.completed_at = datetime.now(UTC)

    def mark_model_call(
        self,
        model_call_id: str,
        status: str,
        *,
        detail: str | None = None,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ModelCallSnapshot:
        if status not in {"invalid", "processed"}:
            raise ValueError("unsupported model call status")
        with self._sessions.begin() as session:
            if claimed_job is not None:
                run_id = session.scalar(
                    select(ModelCallRecord.run_id).where(ModelCallRecord.id == model_call_id)
                )
                if run_id is None:
                    raise ResourceNotFound("model call")
                self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            call = session.scalar(
                select(ModelCallRecord).where(ModelCallRecord.id == model_call_id).with_for_update()
            )
            if call is None:
                raise ResourceNotFound("model call")
            if call.status == status:
                return self._model_call_snapshot(call)
            if call.status != "response_committed":
                raise VersionConflict()
            call.status = status
            call.updated_at = datetime.now(UTC)
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == call.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            data: dict[str, Any] = {"model_call_id": call.id, "turn_id": call.turn_id}
            if detail is not None:
                data["detail"] = detail
            self._append_event_locked(
                session,
                run,
                "model.output_invalid" if status == "invalid" else "model.turn_processed",
                "runner",
                data,
            )
            session.flush()
            return self._model_call_snapshot(call)

    def latest_model_call(self, context: TrustedContext, run_id: str) -> ModelCallSnapshot | None:
        self.get_run(context, run_id)
        with self._sessions() as session:
            call = session.scalar(
                select(ModelCallRecord)
                .where(ModelCallRecord.run_id == run_id)
                .order_by(ModelCallRecord.turn_id.desc())
                .limit(1)
            )
            return None if call is None else self._model_call_snapshot(call)

    def next_turn_id(self, context: TrustedContext, run_id: str) -> int:
        self.get_run(context, run_id)
        with self._sessions() as session:
            maximum = session.scalar(
                select(func.max(ModelCallRecord.turn_id)).where(ModelCallRecord.run_id == run_id)
            )
            return int(maximum or 0) + 1

    def count_model_calls(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        status: str | None = None,
        response_hash: str | None = None,
    ) -> int:
        self.get_run(context, run_id)
        with self._sessions() as session:
            query = (
                select(func.count())
                .select_from(ModelCallRecord)
                .where(ModelCallRecord.run_id == run_id)
            )
            if status is not None:
                query = query.where(ModelCallRecord.status == status)
            if response_hash is not None:
                query = query.where(ModelCallRecord.response_hash == response_hash)
            return int(session.scalar(query) or 0)

    def save_context_snapshot(
        self,
        context: TrustedContext,
        run_id: str,
        turn_id: int,
        content: dict[str, Any],
        source_refs: list[dict[str, Any]],
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> SavedContextSnapshot:
        content_hash = hashlib.sha256(
            json.dumps(
                {"content": content, "source_refs": source_refs},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            self._owned_run_locked(session, context, run_id)
            existing = session.scalar(
                select(ContextSnapshotRecord).where(
                    ContextSnapshotRecord.run_id == run_id,
                    ContextSnapshotRecord.turn_id == turn_id,
                )
            )
            if existing is not None:
                if existing.content_hash != content_hash:
                    raise IdempotencyConflict()
                return self._context_snapshot(existing)
            row = ContextSnapshotRecord(
                id=_identifier("context"),
                run_id=run_id,
                turn_id=turn_id,
                content=content,
                source_refs=source_refs,
                content_hash=content_hash,
            )
            session.add(row)
            session.flush()
            return self._context_snapshot(row)

    def get_context_snapshot(
        self,
        context: TrustedContext,
        run_id: str,
        turn_id: int,
    ) -> SavedContextSnapshot:
        self.get_run(context, run_id)
        with self._sessions() as session:
            row = session.scalar(
                select(ContextSnapshotRecord).where(
                    ContextSnapshotRecord.run_id == run_id,
                    ContextSnapshotRecord.turn_id == turn_id,
                )
            )
            if row is None:
                raise ResourceNotFound("context snapshot")
            return self._context_snapshot(row)

    def reserve_budget(
        self,
        run_id: str,
        *,
        reservation_key: str,
        dimension: str,
        amount: int,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> BudgetReservation:
        if amount <= 0:
            raise ValueError("reservation amount must be positive")
        limit_field, used_field = self._budget_fields(dimension)
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            budget = session.scalar(
                select(BudgetRecord).where(BudgetRecord.run_id == run_id).with_for_update()
            )
            if budget is None:
                raise ResourceNotFound("budget")
            existing = session.scalar(
                select(BudgetReservationRecord).where(
                    BudgetReservationRecord.run_id == run_id,
                    BudgetReservationRecord.reservation_key == reservation_key,
                )
            )
            if existing is not None:
                if existing.dimension != dimension or existing.reserved_amount != amount:
                    raise IdempotencyConflict()
                return self._budget_reservation(existing)
            active = session.scalars(
                select(BudgetReservationRecord).where(
                    BudgetReservationRecord.run_id == run_id,
                    BudgetReservationRecord.dimension == dimension,
                    BudgetReservationRecord.status == "reserved",
                )
            ).all()
            reserved = sum(row.reserved_amount for row in active)
            limit = getattr(budget, limit_field)
            used = int(getattr(budget, used_field))
            if limit is not None and used + reserved + amount > int(limit):
                raise BudgetExhausted(dimension)
            row = BudgetReservationRecord(
                id=_identifier("reservation"),
                run_id=run_id,
                reservation_key=reservation_key,
                dimension=dimension,
                reserved_amount=amount,
                status="reserved",
            )
            session.add(row)
            session.flush()
            return self._budget_reservation(row)

    def settle_budget(
        self,
        reservation_id: str,
        *,
        actual_amount: int,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> BudgetReservation:
        if actual_amount < 0:
            raise ValueError("actual amount cannot be negative")
        with self._sessions.begin() as session:
            run_id = session.scalar(
                select(BudgetReservationRecord.run_id).where(
                    BudgetReservationRecord.id == reservation_id
                )
            )
            if run_id is None:
                raise ResourceNotFound("budget reservation")
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            row = session.scalar(
                select(BudgetReservationRecord)
                .where(BudgetReservationRecord.id == reservation_id)
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFound("budget reservation")
            if row.run_id != run_id:
                raise VersionConflict()
            if row.status == "settled":
                return self._budget_reservation(row)
            if row.status != "reserved":
                raise VersionConflict()
            limit_field, used_field = self._budget_fields(row.dimension)
            budget = session.scalar(
                select(BudgetRecord).where(BudgetRecord.run_id == row.run_id).with_for_update()
            )
            if budget is None:
                raise ResourceNotFound("budget")
            limit = getattr(budget, limit_field)
            used = int(getattr(budget, used_field))
            active_others = session.scalars(
                select(BudgetReservationRecord).where(
                    BudgetReservationRecord.run_id == row.run_id,
                    BudgetReservationRecord.dimension == row.dimension,
                    BudgetReservationRecord.status == "reserved",
                    BudgetReservationRecord.id != row.id,
                )
            ).all()
            still_reserved = sum(item.reserved_amount for item in active_others)
            if limit is not None and used + still_reserved + actual_amount > int(limit):
                raise BudgetExhausted(row.dimension)
            setattr(budget, used_field, used + actual_amount)
            row.actual_amount = actual_amount
            row.status = "settled"
            row.updated_at = datetime.now(UTC)
            session.flush()
            return self._budget_reservation(row)

    def budget_status(self, context: TrustedContext, run_id: str) -> dict[str, Any]:
        self.get_run(context, run_id)
        with self._sessions() as session:
            budget = session.get(BudgetRecord, run_id)
            if budget is None:
                raise ResourceNotFound("budget")
            reservations = session.scalars(
                select(BudgetReservationRecord).where(
                    BudgetReservationRecord.run_id == run_id,
                    BudgetReservationRecord.status == "reserved",
                )
            ).all()
            reserved: dict[str, int] = {}
            for row in reservations:
                reserved[row.dimension] = reserved.get(row.dimension, 0) + int(row.reserved_amount)
            return {
                "limits": {
                    "model_turn": budget.max_model_turns,
                    "tool_call": budget.max_tool_calls,
                    "output_tokens": budget.max_output_tokens,
                    "cost_microunits": budget.max_cost_microunits,
                },
                "used": {
                    "model_turn": budget.used_model_turns,
                    "tool_call": budget.used_tool_calls,
                    "output_tokens": budget.used_output_tokens,
                    "cost_microunits": budget.used_cost_microunits,
                },
                "reserved": reserved,
            }

    def release_budget(
        self,
        reservation_id: str,
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> BudgetReservation:
        with self._sessions.begin() as session:
            run_id = session.scalar(
                select(BudgetReservationRecord.run_id).where(
                    BudgetReservationRecord.id == reservation_id
                )
            )
            if run_id is None:
                raise ResourceNotFound("budget reservation")
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            row = session.scalar(
                select(BudgetReservationRecord)
                .where(BudgetReservationRecord.id == reservation_id)
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFound("budget reservation")
            if row.run_id != run_id:
                raise VersionConflict()
            if row.status == "reserved":
                row.status = "released"
                row.actual_amount = 0
                row.updated_at = datetime.now(UTC)
                session.flush()
            return self._budget_reservation(row)

    def admit_execution_session(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        runtime: str,
        profile_id: str,
        generation: str,
        sandbox_ref: str,
        source_action_id: str,
        expires_at: datetime | None,
    ) -> ExecutionSessionSnapshot:
        with self._sessions.begin() as session:
            run = self._owned_run_locked(session, context, run_id)
            existing = session.scalar(
                select(ExecutionSessionRecord).where(
                    ExecutionSessionRecord.source_action_id == source_action_id
                )
            )
            if existing is not None:
                return self._execution_session_snapshot(existing)
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                raise AlreadyTerminal()
            row = ExecutionSessionRecord(
                id=_identifier("session"),
                run_id=run_id,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                runtime=runtime,
                profile_id=profile_id,
                generation=generation,
                status="ready",
                sandbox_ref=sandbox_ref,
                source_action_id=source_action_id,
                cell_seq=0,
                version=0,
                expires_at=expires_at,
            )
            session.add(row)
            session.flush()
            self._append_event_locked(
                session,
                run,
                "execution.session_ready",
                "execution_broker",
                {
                    "session_id": row.id,
                    "generation": row.generation,
                    "profile_id": row.profile_id,
                },
                action_id=source_action_id,
            )
            session.flush()
            return self._execution_session_snapshot(row)

    def get_execution_session(
        self,
        context: TrustedContext,
        session_id: str,
    ) -> ExecutionSessionSnapshot:
        with self._sessions() as session:
            row = session.scalar(
                select(ExecutionSessionRecord).where(
                    ExecutionSessionRecord.id == session_id,
                    ExecutionSessionRecord.tenant_id == context.tenant_id,
                    ExecutionSessionRecord.subject_id == context.subject_id,
                )
            )
            if row is None:
                raise ResourceNotFound("execution session")
            return self._execution_session_snapshot(row)

    def begin_execution(
        self,
        context: TrustedContext,
        session_id: str,
        *,
        action_id: str,
        generation: str,
        request_hash: str,
        code_hash: str,
    ) -> ExecutionWork:
        with self._sessions.begin() as session:
            execution_session = self._owned_execution_session_locked(session, context, session_id)
            existing = session.scalar(
                select(ExecutionRecord).where(ExecutionRecord.action_id == action_id)
            )
            if existing is not None:
                return ExecutionWork(self._execution_snapshot(existing), True)
            if execution_session.generation != generation:
                raise EnvironmentLost("Execution session generation changed")
            if execution_session.status == "busy":
                raise ExecutionBusy()
            if execution_session.status != "ready":
                raise EnvironmentLost()
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == execution_session.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                raise AlreadyTerminal()
            execution_session.cell_seq += 1
            execution_session.status = "busy"
            execution_session.version += 1
            execution_session.updated_at = datetime.now(UTC)
            row = ExecutionRecord(
                id=_identifier("execution"),
                session_id=execution_session.id,
                action_id=action_id,
                generation=generation,
                cell_index=execution_session.cell_seq,
                request_hash=request_hash,
                code_hash=code_hash,
                status="running",
            )
            session.add(row)
            session.flush()
            self._append_event_locked(
                session,
                run,
                "execution.started",
                "execution_broker",
                {
                    "execution_id": row.id,
                    "session_id": execution_session.id,
                    "generation": generation,
                    "cell_index": row.cell_index,
                },
                action_id=action_id,
            )
            session.flush()
            return ExecutionWork(self._execution_snapshot(row), False)

    def complete_execution(
        self,
        execution_id: str,
        *,
        status: str,
        receipt: dict[str, Any],
    ) -> ExecutionSnapshot:
        allowed = {
            "succeeded",
            "failed",
            "timed_out",
            "output_limit",
            "artifact_limit",
            "environment_lost",
        }
        if status not in allowed:
            raise ValueError("unsupported execution result status")
        with self._sessions.begin() as session:
            row = session.scalar(
                select(ExecutionRecord).where(ExecutionRecord.id == execution_id).with_for_update()
            )
            if row is None:
                raise ResourceNotFound("execution")
            if row.status != "running":
                return self._execution_snapshot(row)
            execution_session = session.scalar(
                select(ExecutionSessionRecord)
                .where(ExecutionSessionRecord.id == row.session_id)
                .with_for_update()
            )
            if execution_session is None:
                raise ResourceNotFound("execution session")
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == execution_session.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            row.status = status
            row.receipt = receipt
            row.completed_at = datetime.now(UTC)
            execution_session.status = (
                "lost" if status in {"timed_out", "environment_lost"} else "ready"
            )
            if execution_session.status == "lost":
                execution_session.sandbox_ref = None
            execution_session.version += 1
            execution_session.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                f"execution.{status}",
                "execution_broker",
                {
                    "execution_id": row.id,
                    "session_id": row.session_id,
                    "generation": row.generation,
                    "cell_index": row.cell_index,
                },
                action_id=row.action_id,
            )
            session.flush()
            return self._execution_snapshot(row)

    def mark_execution_session_lost(
        self,
        context: TrustedContext,
        session_id: str,
        *,
        detail: str,
    ) -> ExecutionSessionSnapshot:
        with self._sessions.begin() as session:
            row = self._owned_execution_session_locked(session, context, session_id)
            if row.status == "lost":
                return self._execution_session_snapshot(row)
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == row.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            row.status = "lost"
            row.sandbox_ref = None
            row.version += 1
            row.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                "execution.session_lost",
                "execution_broker",
                {"session_id": row.id, "generation": row.generation, "detail": detail},
            )
            session.flush()
            return self._execution_session_snapshot(row)

    def replace_execution_session(
        self,
        context: TrustedContext,
        session_id: str,
        *,
        generation: str,
        sandbox_ref: str,
    ) -> ExecutionSessionSnapshot:
        with self._sessions.begin() as session:
            row = self._owned_execution_session_locked(session, context, session_id)
            if row.status not in {"lost", "stopped"}:
                raise VersionConflict()
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == row.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            previous_generation = row.generation
            row.generation = generation
            row.sandbox_ref = sandbox_ref
            row.status = "ready"
            row.version += 1
            row.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                "execution.session_recreated",
                "execution_broker",
                {
                    "session_id": row.id,
                    "previous_generation": previous_generation,
                    "generation": generation,
                },
            )
            session.flush()
            return self._execution_session_snapshot(row)

    def stop_execution_session(
        self,
        context: TrustedContext,
        session_id: str,
    ) -> ExecutionSessionSnapshot:
        with self._sessions.begin() as session:
            row = self._owned_execution_session_locked(session, context, session_id)
            if row.status == "stopped":
                return self._execution_session_snapshot(row)
            run = session.scalar(
                select(RunRecord).where(RunRecord.id == row.run_id).with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run")
            row.status = "stopped"
            row.sandbox_ref = None
            row.version += 1
            row.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                "execution.session_stopped",
                "execution_broker",
                {"session_id": row.id, "generation": row.generation},
            )
            session.flush()
            return self._execution_session_snapshot(row)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        actor_ref: str,
        data: dict[str, Any],
        action_id: str | None = None,
    ) -> EventSnapshot:
        with self._sessions.begin() as session:
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
            if run is None:
                raise ResourceNotFound("run")
            row = self._append_event_locked(
                session, run, event_type, actor_ref, data, action_id=action_id
            )
            session.flush()
            return self._event_snapshot(row)

    def get_event_history(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        after: int,
        limit: int,
    ) -> EventPageSnapshot:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid event history bounds")
        run = self.get_run(context, run_id)
        with self._sessions() as session:
            minimum = session.scalar(
                select(func.min(EventRecord.seq)).where(EventRecord.run_id == run_id)
            )
            if minimum is None:
                if after < run.event_seq:
                    raise EventCursorExpired(run_id, run.event_seq)
            elif after < int(minimum) - 1:
                raise EventCursorExpired(run_id, int(minimum) - 1)
            current_sequence = session.scalar(
                select(RunRecord.event_seq).where(RunRecord.id == run_id)
            )
            if current_sequence is None:
                raise ResourceNotFound("run")
            if after > int(current_sequence):
                raise FutureEventCursor(run_id, int(current_sequence))
            rows = session.scalars(
                select(EventRecord)
                .where(EventRecord.run_id == run_id, EventRecord.seq > after)
                .order_by(EventRecord.seq)
                .limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            visible = rows[:limit]
            next_after = int(visible[-1].seq) if has_more and visible else None
            return EventPageSnapshot(
                tuple(self._event_snapshot(row) for row in visible), next_after
            )

    def get_recent_events(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        limit: int = 20,
    ) -> tuple[EventSnapshot, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("recent event limit must be between 1 and 100")
        self.get_run(context, run_id)
        with self._sessions() as session:
            rows = session.scalars(
                select(EventRecord)
                .where(EventRecord.run_id == run_id)
                .order_by(EventRecord.seq.desc())
                .limit(limit)
            ).all()
            return tuple(self._event_snapshot(row) for row in reversed(rows))

    def record_completion(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        model_call_id: str | None,
        candidate: dict[str, Any],
        verified: bool,
        missing_evidence: list[str],
        acceptance_results: list[dict[str, Any]],
        regression_results: list[dict[str, Any]],
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> str:
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = self._owned_run_locked(session, context, run_id)
            record = CompletionRecord(
                id=_identifier("completion"),
                run_id=run_id,
                model_call_id=model_call_id,
                status="verified" if verified else "missing_evidence",
                candidate=candidate,
                missing_evidence=missing_evidence,
                acceptance_results=acceptance_results,
                regression_results=regression_results,
            )
            session.add(record)
            if verified:
                if self._has_nonterminal_children(session, run_id):
                    raise VersionConflict()
                require_run_transition(RunStatus(run.status), RunStatus.SUCCEEDED)
                run.status = RunStatus.SUCCEEDED.value
                run.version += 1
                run.final_answer = str(candidate["answer"])
                run.result_artifact_ids = list(candidate.get("artifact_ids", []))
                run.updated_at = datetime.now(UTC)
                session.execute(
                    update(JobRecord)
                    .where(JobRecord.run_id == run_id, JobRecord.status == "ready")
                    .values(status="done")
                )
            self._append_event_locked(
                session,
                run,
                "completion.verified" if verified else "completion.rejected",
                "completion_gate",
                {
                    "completion_id": record.id,
                    "missing_evidence": missing_evidence,
                    "acceptance_results": acceptance_results,
                    "regression_results": regression_results,
                },
            )
            session.flush()
            return record.id

    def block_run(
        self,
        run_id: str,
        *,
        code: str,
        detail: str,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> RunSnapshot:
        """Pause a running Run when safe progress needs reconciliation or intervention."""
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
            if run is None:
                raise ResourceNotFound("run")
            if RunStatus(run.status) == RunStatus.BLOCKED:
                return self._snapshot(run)
            require_run_transition(RunStatus(run.status), RunStatus.BLOCKED)
            run.status = RunStatus.BLOCKED.value
            run.version += 1
            run.failure = {"code": code, "detail": detail}
            run.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                "run.blocked",
                "worker",
                {"status": RunStatus.BLOCKED.value, "code": code, "detail": detail},
            )
            session.flush()
            return self._snapshot(run)

    def fail_run(
        self,
        run_id: str,
        *,
        code: str,
        detail: str,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> RunSnapshot:
        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
            if run is None:
                raise ResourceNotFound("run")
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                return self._snapshot(run)
            require_run_transition(RunStatus(run.status), RunStatus.FAILED)
            if self._has_nonterminal_children(session, run_id):
                raise VersionConflict()
            run.status = RunStatus.FAILED.value
            run.version += 1
            run.failure = {"code": code, "detail": detail}
            run.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                "run.failed",
                "runner",
                {"status": RunStatus.FAILED.value, "code": code, "detail": detail},
            )
            session.execute(
                update(JobRecord)
                .where(JobRecord.run_id == run_id, JobRecord.status == "ready")
                .values(status="done")
            )
            session.flush()
            return self._snapshot(run)

    def terminate_run(
        self,
        run_id: str,
        *,
        code: str,
        detail: str,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> RunSnapshot:
        """Stop new work while preserving unresolved external effects for reconciliation."""

        with self._sessions.begin() as session:
            self._require_claimed_run_job_locked(session, claimed_job, worker_id, run_id)
            run = session.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
            if run is None:
                raise ResourceNotFound("run")
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                return self._snapshot(run)
            session.execute(
                update(ActionRecord)
                .where(
                    ActionRecord.run_id == run.id,
                    ActionRecord.status.in_(
                        {
                            ActionStatus.PROPOSED.value,
                            ActionStatus.WAITING_INPUT.value,
                            ActionStatus.READY.value,
                        }
                    ),
                )
                .values(status=ActionStatus.CANCELLED.value)
            )
            session.execute(
                update(BudgetReservationRecord)
                .where(
                    BudgetReservationRecord.run_id == run.id,
                    BudgetReservationRecord.status == "reserved",
                )
                .values(status="released", actual_amount=0, updated_at=datetime.now(UTC))
            )
            unresolved = self._has_uncertain_effects(session, run.id)
            target = RunStatus.BLOCKED if unresolved else RunStatus.FAILED
            require_run_transition(RunStatus(run.status), target)
            if target == RunStatus.FAILED and self._has_nonterminal_children(session, run_id):
                raise VersionConflict()
            run.status = target.value
            run.version += 1
            run.failure = {
                "code": code,
                "detail": detail,
                "termination_intent": True,
                "unresolved_effects": unresolved,
            }
            run.updated_at = datetime.now(UTC)
            self._append_event_locked(
                session,
                run,
                "run.blocked" if unresolved else "run.failed",
                "runner",
                {
                    "status": target.value,
                    "code": code,
                    "detail": detail,
                    "unresolved_effects": unresolved,
                },
            )
            session.execute(
                update(JobRecord)
                .where(JobRecord.run_id == run_id, JobRecord.status == "ready")
                .values(status="done")
            )
            session.flush()
            return self._snapshot(run)

    def _idempotency_query(
        self,
        context: TrustedContext,
        method: str,
        route: str,
        idempotency_key: str,
    ) -> Select[tuple[IdempotencyRecord]]:
        return select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == context.tenant_id,
            IdempotencyRecord.subject_id == context.subject_id,
            IdempotencyRecord.method == method,
            IdempotencyRecord.canonical_route == route,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )

    @staticmethod
    def _owned_run_locked(session: Session, context: TrustedContext, run_id: str) -> RunRecord:
        run = session.scalar(
            select(RunRecord)
            .where(
                RunRecord.id == run_id,
                RunRecord.tenant_id == context.tenant_id,
                RunRecord.subject_id == context.subject_id,
            )
            .with_for_update()
        )
        if run is None:
            raise ResourceNotFound("run")
        return run

    @staticmethod
    def _owned_action_locked(
        session: Session,
        context: TrustedContext,
        action_id: str,
    ) -> ActionRecord:
        action = session.scalar(
            select(ActionRecord)
            .where(
                ActionRecord.id == action_id,
                ActionRecord.tenant_id == context.tenant_id,
                ActionRecord.subject_id == context.subject_id,
            )
            .with_for_update()
        )
        if action is None:
            raise ResourceNotFound("action")
        return action

    @staticmethod
    def _latest_attempt_locked(
        session: Session,
        action_id: str,
        *,
        required_status: str = "started",
    ) -> ActionAttemptRecord:
        attempt = session.scalar(
            select(ActionAttemptRecord)
            .where(ActionAttemptRecord.action_id == action_id)
            .order_by(ActionAttemptRecord.attempt_no.desc())
            .limit(1)
            .with_for_update()
        )
        if attempt is None or attempt.status != required_status:
            raise VersionConflict()
        return attempt

    @staticmethod
    def _has_unresolved_actions(session: Session, run_id: str) -> bool:
        unresolved = session.scalar(
            select(func.count())
            .select_from(ActionRecord)
            .where(
                ActionRecord.run_id == run_id,
                ActionRecord.status.not_in(
                    {
                        ActionStatus.SUCCEEDED.value,
                        ActionStatus.FAILED.value,
                        ActionStatus.CANCELLED.value,
                    }
                ),
            )
        )
        return bool(unresolved)

    @staticmethod
    def _has_nonterminal_children(session: Session, run_id: str) -> bool:
        count = session.scalar(
            select(func.count())
            .select_from(RunRecord)
            .where(
                RunRecord.parent_run_id == run_id,
                RunRecord.status.not_in({status.value for status in TERMINAL_RUN_STATUSES}),
            )
        )
        return bool(count)

    @staticmethod
    def _has_uncertain_effects(session: Session, run_id: str) -> bool:
        uncertain = session.scalar(
            select(func.count())
            .select_from(ActionRecord)
            .where(
                ActionRecord.run_id == run_id,
                ActionRecord.status.in_(
                    {
                        ActionStatus.RUNNING.value,
                        ActionStatus.WAITING_EXTERNAL.value,
                        ActionStatus.RETRY_WAIT.value,
                        ActionStatus.OUTCOME_UNKNOWN.value,
                    }
                ),
            )
        )
        return bool(uncertain)

    @staticmethod
    def _owned_execution_session_locked(
        session: Session,
        context: TrustedContext,
        session_id: str,
    ) -> ExecutionSessionRecord:
        row = session.scalar(
            select(ExecutionSessionRecord)
            .where(
                ExecutionSessionRecord.id == session_id,
                ExecutionSessionRecord.tenant_id == context.tenant_id,
                ExecutionSessionRecord.subject_id == context.subject_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ResourceNotFound("execution session")
        return row

    @staticmethod
    def _append_event_locked(
        session: Session,
        run: RunRecord,
        event_type: str,
        actor_ref: str,
        data: dict[str, Any],
        *,
        action_id: str | None = None,
    ) -> EventRecord:
        run.event_seq += 1
        row = EventRecord(
            id=_identifier("evt"),
            run_id=run.id,
            seq=run.event_seq,
            event_type=event_type,
            actor_ref=actor_ref,
            action_id=action_id,
            data=redact(data),
        )
        session.add(row)
        return row

    @staticmethod
    def _budget_fields(dimension: str) -> tuple[str, str]:
        fields = {
            "model_turn": ("max_model_turns", "used_model_turns"),
            "tool_call": ("max_tool_calls", "used_tool_calls"),
            "output_tokens": ("max_output_tokens", "used_output_tokens"),
            "cost_microunits": ("max_cost_microunits", "used_cost_microunits"),
        }
        try:
            return fields[dimension]
        except KeyError as exc:
            raise ValueError("unsupported budget dimension") from exc

    @staticmethod
    def _lease_matches(
        job: JobRecord | None,
        claimed: ClaimedJob,
        worker_id: str,
        *,
        now: datetime,
    ) -> bool:
        return bool(
            job is not None
            and job.status == "leased"
            and job.lease_owner == worker_id
            and job.lease_epoch == claimed.lease_epoch
            and job.lease_until is not None
            and job.lease_until >= now
        )

    def _require_claimed_job_locked(
        self,
        session: Session,
        claimed_job: ClaimedJob | None,
        worker_id: str | None,
        action_id: str,
    ) -> None:
        if claimed_job is None:
            return
        if worker_id is None:
            raise ValueError("worker_id is required with a claimed job")
        job = session.scalar(
            select(JobRecord).where(JobRecord.id == claimed_job.id).with_for_update()
        )
        now = session.scalar(select(func.now()))
        if now is None:
            raise RuntimeError("database clock is unavailable")
        if (
            not self._lease_matches(job, claimed_job, worker_id, now=now)
            or job is None
            or job.action_id != action_id
            or job.kind != "mcp_task_poll"
        ):
            raise LeaseLost()

    def _require_claimed_run_job_locked(
        self,
        session: Session,
        claimed_job: ClaimedJob | None,
        worker_id: str | None,
        run_id: str,
    ) -> None:
        """Fence model-turn writes to the current advance_run lease, if supplied."""
        if claimed_job is None:
            return
        if worker_id is None:
            raise ValueError("worker_id is required with a claimed job")
        job = session.scalar(
            select(JobRecord).where(JobRecord.id == claimed_job.id).with_for_update()
        )
        now = session.scalar(select(func.now()))
        if now is None:
            raise RuntimeError("database clock is unavailable")
        if (
            not self._lease_matches(job, claimed_job, worker_id, now=now)
            or job is None
            or job.run_id != run_id
            or job.kind != "advance_run"
        ):
            raise LeaseLost()

    def _require_claimed_cancel_job_locked(
        self,
        session: Session,
        claimed_job: ClaimedJob | None,
        worker_id: str | None,
        run_id: str,
    ) -> None:
        """Fence cancellation confirmation to the current cancel_run lease."""
        if claimed_job is None:
            return
        if worker_id is None:
            raise ValueError("worker_id is required with a claimed job")
        job = session.scalar(
            select(JobRecord).where(JobRecord.id == claimed_job.id).with_for_update()
        )
        now = session.scalar(select(func.now()))
        if now is None:
            raise RuntimeError("database clock is unavailable")
        if (
            not self._lease_matches(job, claimed_job, worker_id, now=now)
            or job is None
            or job.run_id != run_id
            or job.kind != "cancel_run"
        ):
            raise LeaseLost()

    @staticmethod
    def _snapshot(run: RunRecord) -> RunSnapshot:
        return RunSnapshot(
            id=run.id,
            status=RunStatus(run.status),
            version=run.version,
            agent_id=run.agent_id,
            conversation_id=run.conversation_id,
            created_at=run.created_at,
            updated_at=run.updated_at,
            effective_limits=dict(run.effective_limits),
            workspace_id=run.workspace_id,
            goal=run.goal,
            artifact_refs=tuple(run.artifact_refs),
            skill_refs=(
                None if run.skill_refs is None else tuple(dict(ref) for ref in run.skill_refs)
            ),
            parent_run_id=run.parent_run_id,
            child_depth=run.child_depth,
            admitted_scopes=frozenset(run.admitted_scopes),
            delegated_scopes=(
                None if run.delegated_scopes is None else frozenset(run.delegated_scopes)
            ),
            result_artifact_ids=tuple(run.result_artifact_ids),
            final_answer=run.final_answer,
            failure=None if run.failure is None else dict(run.failure),
            event_seq=int(run.event_seq),
        )

    @staticmethod
    def _action_snapshot(action: ActionRecord) -> ActionSnapshot:
        return ActionSnapshot(
            id=action.id,
            run_id=action.run_id,
            status=ActionStatus(action.status),
            request_hash=action.request_hash,
            capability_id=action.capability_id,
            capability_revision=action.capability_revision,
            effect_semantics=action.effect_semantics,
            result=None if action.result is None else dict(action.result),
            version=action.version,
            turn_id=action.turn_id,
            call_index=action.call_index,
            bound_operation=dict(action.bound_operation),
            policy_revision=action.policy_revision,
            scope_fingerprint=action.scope_fingerprint,
            approval_request_id=action.approval_request_id,
            downstream_idempotency_key=action.downstream_idempotency_key,
            upstream_handle=action.upstream_handle,
            lease_epoch=action.lease_epoch,
        )

    @staticmethod
    def _model_call_snapshot(call: ModelCallRecord) -> ModelCallSnapshot:
        return ModelCallSnapshot(
            id=call.id,
            run_id=call.run_id,
            turn_id=call.turn_id,
            provider=call.provider,
            model_revision=call.model_revision,
            status=call.status,
            request_payload=dict(call.request_payload),
            response_payload=(
                None if call.response_payload is None else dict(call.response_payload)
            ),
            parsed_output=call.parsed_output,
            response_hash=call.response_hash,
            provider_request_id=call.provider_request_id,
            usage=dict(call.usage),
        )

    @staticmethod
    def _event_snapshot(row: EventRecord) -> EventSnapshot:
        return EventSnapshot(
            event_id=row.id,
            run_id=row.run_id,
            seq=int(row.seq),
            schema_version=row.schema_version,
            type=row.event_type,
            occurred_at=row.created_at,
            action_id=row.action_id,
            data=dict(row.data),
        )

    @staticmethod
    def _context_snapshot(row: ContextSnapshotRecord) -> SavedContextSnapshot:
        return SavedContextSnapshot(
            id=row.id,
            run_id=row.run_id,
            turn_id=row.turn_id,
            content=dict(row.content),
            source_refs=tuple(dict(item) for item in row.source_refs),
            content_hash=row.content_hash,
        )

    @staticmethod
    def _input_request_snapshot(row: InputRequestRecord) -> InputRequestSnapshot:
        return InputRequestSnapshot(
            id=row.id,
            run_id=row.run_id,
            action_id=row.action_id,
            kind=row.kind,
            status=row.status,
            version=row.version,
            prompt=row.prompt,
            request_hash=row.request_hash,
            requested_schema=dict(row.requested_schema),
            expires_at=row.expires_at,
            target_summary=row.target_summary,
            capability_revision=row.capability_revision,
            policy_revision=row.policy_revision,
            scope_fingerprint=row.scope_fingerprint,
            resource_versions=dict(row.resource_versions),
        )

    @staticmethod
    def _mcp_rpc_snapshot(row: MCPRPCRecord) -> MCPRPCSnapshot:
        return MCPRPCSnapshot(
            id=row.id,
            action_id=row.action_id,
            method=row.method,
            sequence=row.sequence,
            correlation_id=row.correlation_id,
            status=row.status,
        )

    @staticmethod
    def _mcp_continuation_snapshot(
        row: MCPContinuationRecord,
        response: InputResponseRecord | None,
    ) -> MCPContinuationSnapshot:
        return MCPContinuationSnapshot(
            id=row.id,
            action_id=row.action_id,
            input_request_id=row.input_request_id,
            integration_id=row.integration_id,
            profile=row.profile,
            tool_name=row.tool_name,
            sequence=row.sequence,
            request_state=row.request_state,
            input_requests=dict(row.input_requests),
            status=row.status,
            response_values=(
                None if response is None or response.values is None else dict(response.values)
            ),
        )

    @staticmethod
    def _mcp_task_snapshot(row: MCPTaskRecord) -> MCPTaskSnapshot:
        return MCPTaskSnapshot(
            id=row.id,
            action_id=row.action_id,
            integration_id=row.integration_id,
            profile=row.profile,
            task_id=row.task_id,
            status=row.status,
            version=row.version,
            created_at_remote=row.created_at_remote,
            last_updated_at_remote=row.last_updated_at_remote,
            ttl_ms=None if row.ttl_ms is None else int(row.ttl_ms),
            poll_interval_ms=(None if row.poll_interval_ms is None else int(row.poll_interval_ms)),
            input_requests=dict(row.input_requests),
            result=None if row.result is None else dict(row.result),
            error=None if row.error is None else dict(row.error),
            cancel_acknowledged=row.cancel_acknowledged,
        )

    @staticmethod
    def _checkpoint_snapshot(row: CheckpointRecord) -> CheckpointSnapshot:
        return CheckpointSnapshot(
            id=row.id,
            run_id=row.run_id,
            checkpoint_seq=row.checkpoint_seq,
            schema_version=row.schema_version,
            capability_id=row.capability_id,
            capability_revision=row.capability_revision,
            policy_revision=row.policy_revision,
            runner_cursor=dict(row.runner_cursor),
            content=dict(row.content),
        )

    @staticmethod
    def _budget_reservation(row: BudgetReservationRecord) -> BudgetReservation:
        return BudgetReservation(
            id=row.id,
            run_id=row.run_id,
            dimension=row.dimension,
            reserved_amount=int(row.reserved_amount),
            status=row.status,
        )

    @staticmethod
    def _execution_session_snapshot(row: ExecutionSessionRecord) -> ExecutionSessionSnapshot:
        return ExecutionSessionSnapshot(
            id=row.id,
            run_id=row.run_id,
            runtime=row.runtime,
            profile_id=row.profile_id,
            generation=row.generation,
            status=row.status,
            sandbox_ref=row.sandbox_ref,
            cell_seq=row.cell_seq,
            version=row.version,
            expires_at=row.expires_at,
        )

    @staticmethod
    def _execution_snapshot(row: ExecutionRecord) -> ExecutionSnapshot:
        return ExecutionSnapshot(
            id=row.id,
            session_id=row.session_id,
            action_id=row.action_id,
            generation=row.generation,
            cell_index=row.cell_index,
            request_hash=row.request_hash,
            code_hash=row.code_hash,
            status=row.status,
            receipt=None if row.receipt is None else dict(row.receipt),
        )
