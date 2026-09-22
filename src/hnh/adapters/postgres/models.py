from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from hnh.domain.states import ActionStatus, RunStatus

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum_values(enum_type: type[RunStatus] | type[ActionStatus]) -> str:
    return ", ".join(f"'{item.value}'" for item in enum_type)


class RunRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_enum_values(RunStatus)})", name="status_valid"),
        Index("ix_runs_owner_created", "tenant_id", "subject_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(128))
    workspace_id: Mapped[str | None] = mapped_column(String(128))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    # Admission-time upper bound only. Worker authorization must also obtain
    # the actor's current scopes from a trusted authentication provider.
    admitted_scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    artifact_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    skill_refs: Mapped[list[dict[str, str]] | None] = mapped_column(JSON)
    parent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), index=True
    )
    child_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delegated_scopes: Mapped[list[str] | None] = mapped_column(JSON)
    effective_limits: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="dev-1")
    cancel_reason: Mapped[str | None] = mapped_column(String(1024))
    final_answer: Mapped[str | None] = mapped_column(Text)
    result_artifact_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ActionRecord(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("run_id", "turn_id", "call_index", name="action_slot"),
        CheckConstraint(f"status IN ({_enum_values(ActionStatus)})", name="status_valid"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False, default="legacy")
    capability_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    effect_semantics: Mapped[str] = mapped_column(String(32), nullable=False)
    bound_operation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="dev-1")
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approval_request_id: Mapped[str | None] = mapped_column(String(64))
    downstream_idempotency_key: Mapped[str | None] = mapped_column(String(256))
    upstream_handle: Mapped[str | None] = mapped_column(String(256))
    cancel_receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ActionAttemptRecord(Base):
    __tablename__ = "action_attempts"
    __table_args__ = (
        UniqueConstraint("action_id", "attempt_no", name="action_attempt"),
        CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'outcome_unknown')",
            name="status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    downstream_idempotency_key: Mapped[str | None] = mapped_column(String(256))
    dispatch_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dispatch_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_ref: Mapped[str | None] = mapped_column(String(256))
    upstream_handle: Mapped[str | None] = mapped_column(String(256))


class ModelCallRecord(Base):
    __tablename__ = "model_calls"
    __table_args__ = (UniqueConstraint("run_id", "turn_id", name="model_turn"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_ref: Mapped[str | None] = mapped_column(String(256))
    result_ref: Mapped[str | None] = mapped_column(String(256))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    parsed_output: Mapped[Any | None] = mapped_column(JSON)
    response_hash: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ModelCallAttemptRecord(Base):
    __tablename__ = "model_call_attempts"
    __table_args__ = (UniqueConstraint("model_call_id", "attempt_no", name="model_call_attempt"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_call_id: Mapped[str] = mapped_column(
        ForeignKey("model_calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventRecord(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="run_sequence"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    actor_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="job_dedupe"),
        CheckConstraint("status IN ('ready', 'leased', 'done')", name="status_valid"),
        Index("ix_jobs_claim", "status", "due_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "method",
            "canonical_route",
            "idempotency_key",
            name="uq_idempotency_records_request_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_route: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[str] = mapped_column(String(512), nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InputRequestRecord(Base):
    __tablename__ = "input_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('approval', 'clarification', 'external_input')",
            name="kind_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'answered', 'denied', 'expired')",
            name="status_valid",
        ),
        Index("ix_input_requests_owner_status", "tenant_id", "subject_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    target_summary: Mapped[str | None] = mapped_column(Text)
    capability_revision: Mapped[str | None] = mapped_column(String(128))
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_versions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InputResponseRecord(Base):
    __tablename__ = "input_responses"
    __table_args__ = (
        UniqueConstraint("input_request_id", name="input_response_once"),
        UniqueConstraint(
            "tenant_id", "subject_id", "idempotency_key", name="input_response_idempotency"
        ),
        CheckConstraint("decision IN ('approve', 'deny', 'submit')", name="decision_valid"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_request_id: Mapped[str] = mapped_column(
        ForeignKey("input_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    values: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReconciliationRecord(Base):
    __tablename__ = "reconciliations"
    __table_args__ = (
        UniqueConstraint("action_id", "idempotency_key", name="reconciliation_idempotency"),
        CheckConstraint(
            "resolution IN ('confirmed_applied', 'confirmed_not_applied', 'confirmed_stopped')",
            name="resolution_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CheckpointRecord(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "checkpoint_seq", name="run_checkpoint"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    capability_id: Mapped[str | None] = mapped_column(String(128))
    capability_revision: Mapped[str | None] = mapped_column(String(128))
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_cursor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MCPRPCRecord(Base):
    __tablename__ = "mcp_rpc_calls"
    __table_args__ = (
        UniqueConstraint("action_id", "sequence", name="mcp_rpc_action_sequence"),
        CheckConstraint(
            "status IN ('started', 'complete', 'protocol_error', "
            "'input_required', 'task', 'outcome_unknown')",
            name="status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    integration_id: Mapped[str] = mapped_column(String(128), nullable=False)
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MCPContinuationRecord(Base):
    __tablename__ = "mcp_continuations"
    __table_args__ = (
        UniqueConstraint("action_id", "sequence", name="mcp_continuation_sequence"),
        UniqueConstraint("input_request_id", name="mcp_continuation_input_request"),
        CheckConstraint("status IN ('pending', 'answered', 'resumed')", name="status_valid"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_request_id: Mapped[str] = mapped_column(
        ForeignKey("input_requests.id", ondelete="CASCADE"), nullable=False
    )
    integration_id: Mapped[str] = mapped_column(String(128), nullable=False)
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    request_state: Mapped[str | None] = mapped_column(Text)
    input_requests: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MCPTaskRecord(Base):
    __tablename__ = "mcp_tasks"
    __table_args__ = (
        UniqueConstraint("action_id", name="mcp_task_action"),
        UniqueConstraint("integration_id", "profile", "task_id", name="mcp_task_remote"),
        CheckConstraint(
            "status IN ('working', 'input_required', 'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    integration_id: Mapped[str] = mapped_column(String(128), nullable=False)
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_message: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at_remote: Mapped[str] = mapped_column(String(64), nullable=False)
    last_updated_at_remote: Mapped[str] = mapped_column(String(64), nullable=False)
    ttl_ms: Mapped[int | None] = mapped_column(BigInteger)
    poll_interval_ms: Mapped[int | None] = mapped_column(BigInteger)
    input_requests: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cancel_acknowledged: Mapped[bool] = mapped_column(default=False, nullable=False)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BudgetRecord(Base):
    __tablename__ = "budgets"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    max_model_turns: Mapped[int | None] = mapped_column(Integer)
    max_tool_calls: Mapped[int | None] = mapped_column(Integer)
    max_wall_seconds: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    max_cost_microunits: Mapped[int | None] = mapped_column(BigInteger)
    used_model_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_cost_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class BudgetReservationRecord(Base):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint("run_id", "reservation_key", name="budget_reservation_key"),
        CheckConstraint("status IN ('reserved', 'settled', 'released')", name="status_valid"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reservation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_amount: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ContextSnapshotRecord(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (UniqueConstraint("run_id", "turn_id", name="context_turn"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompletionRecord(Base):
    __tablename__ = "completion_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_calls.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    missing_evidence: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    acceptance_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    regression_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionSessionRecord(Base):
    __tablename__ = "execution_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('starting', 'ready', 'busy', 'lost', 'stopped')",
            name="status_valid",
        ),
        UniqueConstraint("source_action_id", name="execution_session_source_action"),
        Index("ix_execution_sessions_owner", "tenant_id", "subject_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sandbox_ref: Mapped[str | None] = mapped_column(String(128))
    source_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id", ondelete="SET NULL")
    )
    cell_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutionRecord(Base):
    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out', "
            "'output_limit', 'artifact_limit', 'environment_lost')",
            name="status_valid",
        ),
        UniqueConstraint("session_id", "cell_index", name="execution_cell"),
        UniqueConstraint("action_id", name="execution_action"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("execution_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id", ondelete="SET NULL"))
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    cell_index: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_owner_created", "tenant_id", "subject_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    storage_key: Mapped[str | None] = mapped_column(String(128))
    source_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceEntryRecord(Base):
    __tablename__ = "workspace_entries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "subject_id", "workspace_id", "logical_path", name="workspace_path"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    logical_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    etag: Mapped[str] = mapped_column(String(160), nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class HttpExchangeRecord(Base):
    __tablename__ = "http_exchanges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transport_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
