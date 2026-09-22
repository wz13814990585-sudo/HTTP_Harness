from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

MCPProfile = Literal["modern2026-07-28", "legacy2025-11-25"]
MCPTaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class MCPToolDescriptor:
    name: str
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MCPDiscovery:
    protocol_version: str
    profile: MCPProfile
    tools: tuple[MCPToolDescriptor, ...]
    extensions: frozenset[str] = frozenset()
    ttl_ms: int = 0
    cache_scope: str = "private"
    server_info: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MCPComplete:
    content: tuple[dict[str, Any], ...]
    structured_content: Any = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class MCPInputRequired:
    input_requests: dict[str, dict[str, Any]]
    request_state: str | None


@dataclass(frozen=True, slots=True)
class MCPTask:
    task_id: str
    status: MCPTaskStatus
    created_at: str
    last_updated_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None = None
    status_message: str | None = None
    input_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


MCPInvocation = MCPComplete | MCPInputRequired | MCPTask


class MCPProtocolFault(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPClient(Protocol):
    def discover(self) -> MCPDiscovery: ...

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        input_responses: dict[str, dict[str, Any]] | None = None,
        request_state: str | None = None,
    ) -> MCPInvocation: ...

    def get_task(self, task_id: str) -> MCPTask: ...

    def update_task(
        self,
        task_id: str,
        input_responses: dict[str, dict[str, Any]],
    ) -> None: ...

    def cancel_task(self, task_id: str) -> None: ...
