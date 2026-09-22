from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LimitsModel(StrictModel):
    max_model_turns: int | None = Field(default=None, ge=1, le=200)
    max_tool_calls: int | None = Field(default=None, ge=1, le=1000)
    max_wall_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_output_tokens: int | None = Field(default=None, ge=1, le=65536)
    max_cost_microunits: int | None = Field(default=None, ge=0)


class RunCreateModel(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    input: str = Field(min_length=1, max_length=65536)
    conversation_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )
    workspace_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )
    artifact_refs: list[str] = Field(default_factory=list, max_length=100)
    limits: LimitsModel = Field(default_factory=LimitsModel)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict):
            optional_fields = {"conversation_id", "workspace_id", "artifact_refs", "limits"}
            if any(key in value and value[key] is None for key in optional_fields):
                raise ValueError("optional fields must be omitted rather than null")
        return value


class CancelCreateModel(StrictModel):
    reason: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        if isinstance(value, dict) and "reason" in value and value["reason"] is None:
            raise ValueError("reason must be omitted rather than null")
        return value


class AcceptedModel(StrictModel):
    resource_id: str
    location: str


class RunModel(StrictModel):
    id: str
    status: str
    version: int
    agent_id: str
    conversation_id: str | None = None
    created_at: datetime
    updated_at: datetime
    pending_input_ids: list[str] = Field(default_factory=list)
    unresolved_action_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    final_answer: str | None = None
    failure: dict[str, Any] | None = None
    effective_limits: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snapshot: Any, actions: tuple[Any, ...] = ()) -> Self:
        return cls(
            id=snapshot.id,
            status=snapshot.status.value,
            version=snapshot.version,
            agent_id=snapshot.agent_id,
            conversation_id=snapshot.conversation_id,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            unresolved_action_ids=[
                action.id for action in actions if action.status.value != "succeeded"
            ],
            artifact_ids=list(snapshot.result_artifact_ids),
            final_answer=snapshot.final_answer,
            failure=snapshot.failure,
            effective_limits=snapshot.effective_limits,
        )


class EventModel(StrictModel):
    event_id: str
    run_id: str
    seq: int = Field(ge=1)
    schema_version: str
    type: str
    occurred_at: datetime
    action_id: str | None = None
    data: dict[str, Any]


class EventPageModel(StrictModel):
    events: list[EventModel]
    next_after: int | None = Field(default=None, ge=0)


class SessionCreateModel(StrictModel):
    run_id: str = Field(min_length=1, max_length=128)
    runtime: str = Field(pattern=r"^python$")
    profile_id: str = Field(min_length=1, max_length=128)


class ExecutionSessionModel(StrictModel):
    id: str
    run_id: str
    generation: str
    status: str
    runtime: str
    expires_at: datetime | None = None


class InputRequestModel(StrictModel):
    id: str
    run_id: str
    action_id: str | None = None
    kind: str
    status: str
    version: int
    prompt: str
    request_hash: str
    requested_schema: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime
    target_summary: str | None = None


class InputResponseModel(StrictModel):
    decision: Literal["approve", "deny", "submit"]
    values: dict[str, Any] | None = None
    comment: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_decision_shape(self) -> Self:
        if self.decision == "submit" and self.values is None:
            raise ValueError("submit requires values")
        if self.decision != "submit" and self.values is not None:
            raise ValueError("approval decisions cannot include values")
        return self


class ReconciliationModel(StrictModel):
    action_id: str = Field(min_length=1, max_length=128)
    resolution: Literal[
        "confirmed_applied",
        "confirmed_not_applied",
        "confirmed_stopped",
    ]
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=4096)
