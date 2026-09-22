from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Self

from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from mcp_types import (
    CallToolResult,
    EmptyResult,
    InputResponses,
    Request,
    RequestParams,
    Result,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import model_validator

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
TaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]


class TaskFields(Result):
    task_id: str
    status: TaskStatus
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None = None


class CreateTaskResult(TaskFields):
    """The initial extension result contains only the base Task fields."""

    result_type: Literal["task"] = "task"


class GetTaskResult(TaskFields):
    """tasks/get may add the status-specific detailed task payload."""

    result_type: Literal["complete"] = "complete"
    input_requests: dict[str, dict[str, Any]] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        if self.status == "input_required" and not self.input_requests:
            raise ValueError("input_required task must include inputRequests")
        if self.status == "completed" and self.result is None:
            raise ValueError("completed task must include result")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed task must include error")
        return self


class TaskRequestParams(RequestParams):
    task_id: str


class GetTaskRequest(Request[TaskRequestParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    params: TaskRequestParams
    name_param = "taskId"


class UpdateTaskRequestParams(TaskRequestParams):
    input_responses: InputResponses


class UpdateTaskRequest(Request[UpdateTaskRequestParams, Literal["tasks/update"]]):
    method: Literal["tasks/update"] = "tasks/update"
    params: UpdateTaskRequestParams
    name_param = "taskId"


class CancelTaskRequest(Request[TaskRequestParams, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    params: TaskRequestParams
    name_param = "taskId"


class TaskAckResult(EmptyResult):
    result_type: Literal["complete"] = "complete"


async def _manual_task_resolution(
    _result: CreateTaskResult,
    _context: ClaimContext,
) -> CallToolResult:
    raise RuntimeError("task results must be persisted before polling")


class TasksClientExtension(ClientExtension):
    identifier = TASKS_EXTENSION_ID

    def claims(self) -> Sequence[ResultClaim[Any]]:
        return (
            ResultClaim(
                result_type="task",
                model=CreateTaskResult,
                resolve=_manual_task_resolution,
                protocol_versions=frozenset(MODERN_PROTOCOL_VERSIONS),
            ),
        )
