from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import Client
from mcp.client.session import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ElicitRequestParams,
    ElicitResult,
    InputRequiredResult,
    InputResponses,
)

from hnh.adapters.mcp.tasks import (
    TASKS_EXTENSION_ID,
    CancelTaskRequest,
    CreateTaskResult,
    GetTaskRequest,
    GetTaskResult,
    TaskAckResult,
    TaskRequestParams,
    TasksClientExtension,
    UpdateTaskRequest,
    UpdateTaskRequestParams,
)
from hnh.application.credentials import CredentialProvider
from hnh.application.mcp_catalog import MCPIntegrationBinding
from hnh.domain.errors import InvalidOperation
from hnh.ports.mcp import (
    MCPComplete,
    MCPDiscovery,
    MCPInputRequired,
    MCPInvocation,
    MCPProtocolFault,
    MCPTask,
    MCPToolDescriptor,
)


class OfficialSDKMCPClient:
    """MCP 2.x adapter with explicit profile and optional formal Tasks extension."""

    def __init__(
        self,
        server: Any,
        binding: MCPIntegrationBinding,
        *,
        subject_id: str,
        tenant_id: str | None = None,
        credential_vault: CredentialProvider | None = None,
        http_transport_factory: Callable[[str, dict[str, str]], AbstractAsyncContextManager[Any]]
        | None = None,
        oauth_transport_factory: Callable[[str], AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self.server = server
        self.binding = binding
        self.subject_id = subject_id
        self.tenant_id = tenant_id
        self.credential_vault = credential_vault
        self.http_transport_factory = http_transport_factory or self._http_transport
        self.oauth_transport_factory = oauth_transport_factory

    def discover(self) -> MCPDiscovery:
        return anyio.run(self._discover)

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        input_responses: dict[str, dict[str, Any]] | None = None,
        request_state: str | None = None,
    ) -> MCPInvocation:
        return anyio.run(
            self._call_tool,
            tool_name,
            arguments,
            input_responses,
            request_state,
        )

    def get_task(self, task_id: str) -> MCPTask:
        return anyio.run(self._get_task, task_id)

    def update_task(
        self,
        task_id: str,
        input_responses: dict[str, dict[str, Any]],
    ) -> None:
        anyio.run(self._update_task, task_id, input_responses)

    def cancel_task(self, task_id: str) -> None:
        anyio.run(self._cancel_task, task_id)

    async def _discover(self) -> MCPDiscovery:
        try:
            async with self._client() as client:
                tools = await client.list_tools(cache_mode="bypass")
                self._validate_profile(client.protocol_version)
                capabilities = client.server_capabilities
                extensions = frozenset((capabilities.extensions or {}).keys())
                server_info = (
                    None
                    if client.server_info is None
                    else client.server_info.model_dump(by_alias=True, mode="json")
                )
                discover_result = client.session.discover_result
                ttl_values = [tools.ttl_ms]
                if discover_result is not None:
                    ttl_values.append(discover_result.ttl_ms)
                ttl_ms = min(ttl_values) if ttl_values else 0
                return MCPDiscovery(
                    protocol_version=client.protocol_version,
                    profile=self.binding.profile,
                    tools=tuple(
                        MCPToolDescriptor(
                            name=tool.name,
                            description=tool.description,
                            input_schema=tool.input_schema,
                            output_schema=tool.output_schema,
                            annotations=(
                                None
                                if tool.annotations is None
                                else tool.annotations.model_dump(
                                    by_alias=True, mode="json", exclude_none=True
                                )
                            ),
                        )
                        for tool in tools.tools
                    ),
                    extensions=extensions,
                    ttl_ms=ttl_ms,
                    cache_scope=tools.cache_scope,
                    server_info=server_info,
                )
        except MCPError as exc:
            raise MCPProtocolFault(exc.code, exc.message, exc.data) from exc

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        input_responses: dict[str, dict[str, Any]] | None,
        request_state: str | None,
    ) -> MCPInvocation:
        try:
            async with self._client() as client:
                self._validate_profile(client.protocol_version)
                await client.list_tools(cache_mode="bypass")
                result = await client.session.call_tool(
                    tool_name,
                    arguments,
                    input_responses=cast(InputResponses | None, input_responses),
                    request_state=request_state,
                    allow_input_required=True,
                    allow_claimed=True,
                )
                return self._map_invocation(result)
        except MCPError as exc:
            raise MCPProtocolFault(exc.code, exc.message, exc.data) from exc
        except ExceptionGroup as exc:
            fault = self._group_protocol_fault(exc)
            if fault is None:
                raise
            raise fault from exc

    async def _get_task(self, task_id: str) -> MCPTask:
        self._require_tasks()
        try:
            async with self._client() as client:
                self._validate_profile(client.protocol_version)
                result = await client.session.send_request(
                    GetTaskRequest(params=TaskRequestParams(task_id=task_id)),
                    GetTaskResult,
                )
                return self._map_task(result)
        except MCPError as exc:
            raise MCPProtocolFault(exc.code, exc.message, exc.data) from exc

    async def _update_task(
        self,
        task_id: str,
        input_responses: dict[str, dict[str, Any]],
    ) -> None:
        self._require_tasks()
        try:
            async with self._client() as client:
                self._validate_profile(client.protocol_version)
                await client.session.send_request(
                    UpdateTaskRequest(
                        params=UpdateTaskRequestParams(
                            task_id=task_id,
                            input_responses=cast(InputResponses, input_responses),
                        )
                    ),
                    TaskAckResult,
                )
        except MCPError as exc:
            raise MCPProtocolFault(exc.code, exc.message, exc.data) from exc

    async def _cancel_task(self, task_id: str) -> None:
        self._require_tasks()
        try:
            async with self._client() as client:
                self._validate_profile(client.protocol_version)
                await client.session.send_request(
                    CancelTaskRequest(params=TaskRequestParams(task_id=task_id)),
                    TaskAckResult,
                )
        except MCPError as exc:
            raise MCPProtocolFault(exc.code, exc.message, exc.data) from exc

    def _client(self) -> Client:
        extensions = [TasksClientExtension()] if self.binding.tasks_enabled else None
        mode = "auto" if self.binding.profile == "modern2026-07-28" else "legacy"
        return Client(
            self._server_or_transport(),
            mode=mode,
            extensions=extensions,
            elicitation_callback=(
                self._no_inline_elicitation if self.binding.profile == "modern2026-07-28" else None
            ),
            cache=None,
        )

    @staticmethod
    async def _no_inline_elicitation(
        context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult:
        del context, params
        raise RuntimeError("MCP elicitation must be persisted as an InputRequest")

    @staticmethod
    def _group_protocol_fault(group: ExceptionGroup) -> MCPProtocolFault | None:
        leaves: list[Exception] = []

        def visit(item: Exception) -> None:
            if isinstance(item, ExceptionGroup):
                for nested in item.exceptions:
                    visit(nested)
            else:
                leaves.append(item)

        visit(group)
        if len(leaves) == 1 and isinstance(leaves[0], MCPError):
            error = leaves[0]
            return MCPProtocolFault(error.code, error.message, error.data)
        return None

    def _server_or_transport(self) -> Any:
        if not isinstance(self.server, str):
            return self.server
        parsed = urlsplit(self.server)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise InvalidOperation("MCP endpoint must be a trusted HTTPS URL")
        headers: dict[str, str] = {}
        if self.binding.credential_reference is not None:
            if (
                self.tenant_id is None
                or self.binding.issuer is None
                or self.binding.resource is None
            ):
                raise RuntimeError("credential binding is incomplete")
            audience = urlsplit(self.binding.resource)
            audience_path = audience.path.rstrip("/")
            if (
                audience.scheme != parsed.scheme
                or audience.netloc != parsed.netloc
                or (
                    audience_path
                    and parsed.path != audience_path
                    and not parsed.path.startswith(audience_path + "/")
                )
            ):
                raise InvalidOperation("MCP endpoint does not match the credential resource")
            if self.oauth_transport_factory is not None:
                return self.oauth_transport_factory(self.server)
            if self.credential_vault is None:
                raise RuntimeError("credential binding is incomplete")
            token = self.credential_vault.resolve(
                self.binding.credential_reference,
                integration_id=self.binding.integration_id,
                tenant_id=self.tenant_id,
                issuer=self.binding.issuer,
                resource=self.binding.resource,
                subject_id=self.subject_id,
            )
            headers["Authorization"] = f"Bearer {token}"
        return self.http_transport_factory(self.server, headers)

    @staticmethod
    @asynccontextmanager
    async def _http_transport(
        url: str,
        headers: dict[str, str],
    ) -> AsyncIterator[Any]:
        async with httpx2.AsyncClient(
            headers=headers,
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                yield streams

    def _validate_profile(self, protocol_version: str) -> None:
        expected = "2026-07-28" if self.binding.profile == "modern2026-07-28" else "2025-11-25"
        if protocol_version != expected:
            raise MCPProtocolFault(
                -32022,
                f"configured MCP profile expected {expected}, negotiated {protocol_version}",
            )

    def _require_tasks(self) -> None:
        if not self.binding.tasks_enabled or self.binding.profile != "modern2026-07-28":
            raise MCPProtocolFault(-32601, "Tasks extension is not enabled for this profile")

    @staticmethod
    def _map_invocation(result: Any) -> MCPInvocation:
        if isinstance(result, CallToolResult):
            return MCPComplete(
                content=tuple(
                    item.model_dump(by_alias=True, mode="json", exclude_none=True)
                    for item in result.content
                ),
                structured_content=result.structured_content,
                is_error=result.is_error,
            )
        if isinstance(result, InputRequiredResult):
            return MCPInputRequired(
                input_requests={
                    key: request.model_dump(by_alias=True, mode="json", exclude_none=True)
                    for key, request in (result.input_requests or {}).items()
                },
                request_state=result.request_state,
            )
        if isinstance(result, CreateTaskResult):
            return OfficialSDKMCPClient._map_task(result)
        raise MCPProtocolFault(-32603, f"unsupported MCP result type {type(result).__name__}")

    @staticmethod
    def _map_task(result: CreateTaskResult | GetTaskResult) -> MCPTask:
        return MCPTask(
            task_id=result.task_id,
            status=result.status,
            created_at=result.created_at,
            last_updated_at=result.last_updated_at,
            ttl_ms=result.ttl_ms,
            poll_interval_ms=result.poll_interval_ms,
            status_message=result.status_message,
            input_requests=(result.input_requests or {})
            if isinstance(result, GetTaskResult)
            else {},
            result=result.result if isinstance(result, GetTaskResult) else None,
            error=result.error if isinstance(result, GetTaskResult) else None,
        )


__all__ = ["TASKS_EXTENSION_ID", "OfficialSDKMCPClient"]
