from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.streamable_http import streamable_http_client
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import Elicit, MCPServer, Resolve, require_client_extension
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import HEADER_MISMATCH, CallToolRequestParams, CallToolResult, TextContent
from pydantic import BaseModel
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from hnh.adapters.mcp.oauth import InMemoryOAuthTokenStorage, IssuerPinnedOAuthMCPTransport
from hnh.adapters.mcp.sdk import OfficialSDKMCPClient
from hnh.adapters.mcp.tasks import (
    TASKS_EXTENSION_ID,
    CreateTaskResult,
    GetTaskResult,
    TaskAckResult,
    TaskRequestParams,
    UpdateTaskRequestParams,
)
from hnh.adapters.postgres.models import (
    ActionRecord,
    JobRecord,
    MCPContinuationRecord,
    MCPRPCRecord,
    MCPTaskRecord,
)
from hnh.application.action_gateway import ActionGateway
from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.credentials import (
    CredentialBinding,
    InMemoryCredentialVault,
    InMemoryOAuthClientVault,
    OAuthClientCredentialBinding,
)
from hnh.application.mcp import MCPActionCoordinator
from hnh.application.mcp_catalog import MCPIntegrationBinding, MCPToolCatalog
from hnh.application.mcp_composition import MCPIntegrationManager
from hnh.application.operations import HttpOperation
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.domain.errors import HarnessError, InvalidOperation, LeaseLost, PermissionDenied
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.ports.mcp import MCPDiscovery, MCPToolDescriptor
from hnh.transport.http.app import create_app

CONTEXT = TrustedContext(
    "tenant-p06",
    "subject-p06",
    frozenset({"integrations:invoke", "inputs:read", "inputs:respond", "runs:read"}),
)


class Details(BaseModel):
    name: str


def ask_details() -> Elicit[Details]:
    return Elicit("Who should be greeted?", Details)


class RequestObserver:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | int | None]] = []

    async def __call__(
        self,
        context: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        self.requests.append((context.method, context.request_id))
        return await call_next(context)


class TestTasksExtension(Extension):
    __test__ = False
    identifier = TASKS_EXTENSION_ID

    def __init__(self) -> None:
        self.created = 0
        self.gets = 0
        self.updates = 0
        self.cancels = 0
        self.status: dict[str, str] = {}
        self.updated: set[str] = set()
        self.cancel_requested: set[str] = set()

    def methods(self) -> Sequence[MethodBinding]:
        versions = frozenset({"2026-07-28"})
        return (
            MethodBinding("tasks/get", TaskRequestParams, self.get, versions),
            MethodBinding("tasks/update", UpdateTaskRequestParams, self.update, versions),
            MethodBinding("tasks/cancel", TaskRequestParams, self.cancel, versions),
        )

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        context: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if params.name == "protocol_fault":
            raise MCPError(code=-32099, message="synthetic protocol fault", data={"kind": "test"})
        if params.name == "array_result":
            return CallToolResult(
                content=[TextContent(type="text", text="array")],
                structured_content=[1, {"nested": True}],
            )
        if params.name != "task_tool":
            if params.name == "task_immediate":
                self.created += 1
                task_id = f"task-{self.created}"
                self.status[task_id] = "completed"
                return self._create(task_id, "completed")
            return await call_next(context)
        require_client_extension(context, self.identifier)
        self.created += 1
        task_id = f"task-{self.created}"
        self.status[task_id] = "working"
        return self._create(task_id, "working")

    async def get(
        self,
        context: ServerRequestContext[Any, Any],
        params: TaskRequestParams,
    ) -> HandlerResult:
        require_client_extension(context, self.identifier)
        self.gets += 1
        task_id = params.task_id
        if task_id in self.cancel_requested:
            self.status[task_id] = "cancelled"
        elif task_id in self.updated:
            self.status[task_id] = "completed"
        elif self.status[task_id] == "working":
            self.status[task_id] = "input_required"
        status = self.status[task_id]
        return self._get(task_id, status)

    async def update(
        self,
        context: ServerRequestContext[Any, Any],
        params: UpdateTaskRequestParams,
    ) -> HandlerResult:
        require_client_extension(context, self.identifier)
        assert params.input_responses["details"].action == "accept"
        self.updates += 1
        self.updated.add(params.task_id)
        self.status[params.task_id] = "working"
        return TaskAckResult()

    async def cancel(
        self,
        context: ServerRequestContext[Any, Any],
        params: TaskRequestParams,
    ) -> HandlerResult:
        require_client_extension(context, self.identifier)
        self.cancels += 1
        self.cancel_requested.add(params.task_id)
        return TaskAckResult()

    @staticmethod
    def _fields(task_id: str, status: str) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "task_id": task_id,
            "status": status,
            "created_at": "2026-09-22T00:00:00Z",
            "last_updated_at": datetime.now(UTC).isoformat(),
            "ttl_ms": 60000,
            "poll_interval_ms": 0,
        }
        if status == "input_required":
            fields["input_requests"] = {
                "details": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": "Provide task details",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    },
                }
            }
        if status == "completed":
            fields["result"] = {
                "content": [{"type": "text", "text": "task complete"}],
                "structuredContent": {"done": True},
                "isError": False,
            }
        return fields

    @classmethod
    def _create(cls, task_id: str, status: str) -> CreateTaskResult:
        return CreateTaskResult(**cls._fields(task_id, status))

    @classmethod
    def _get(cls, task_id: str, status: str) -> GetTaskResult:
        return GetTaskResult(**cls._fields(task_id, status))


def make_server() -> tuple[MCPServer[Any], RequestObserver, TestTasksExtension]:
    observer = RequestObserver()
    tasks = TestTasksExtension()
    server: MCPServer[Any] = MCPServer(
        "p06-official-server",
        version="1.0.0",
        extensions=[tasks],
        middleware=[observer],
    )

    @server.tool(structured_output=True)
    def echo(message: str) -> dict[str, Any]:
        return {"message": message, "nested": {"numbers": [1, 2, 3]}}

    @server.tool()
    def tool_error() -> str:
        raise ValueError("tool-level failure")

    @server.tool()
    def protocol_fault() -> str:
        return "unreachable"

    @server.tool(structured_output=False)
    def array_result() -> str:
        return "unreachable"

    @server.tool()
    def greet(details: Annotated[Details, Resolve(ask_details)]) -> str:
        return f"Hello {details.name}"

    @server.tool()
    def task_tool() -> str:
        return "unreachable"

    @server.tool()
    def task_immediate() -> str:
        return "unreachable"

    return server, observer, tasks


def binding(*, tasks: bool = True, profile: str = "modern2026-07-28") -> MCPIntegrationBinding:
    return MCPIntegrationBinding(
        integration_id="official",
        profile=profile,  # type: ignore[arg-type]
        tasks_enabled=tasks,
        tool_effects={
            "echo": "read_only",
            "array_result": "read_only",
            "greet": "read_only",
            "protocol_fault": "read_only",
            "tool_error": "read_only",
            "task_tool": "remote_idempotent",
            "task_immediate": "remote_idempotent",
        },
    )


def operation(tool: str, value: dict[str, Any]) -> HttpOperation:
    return HttpOperation.model_validate(
        {
            "method": "POST",
            "target": f"/v1/integrations/official/operations/{tool}/invocations",
            "headers": {"content-type": "application/json"},
            "payload": {"kind": "json", "value": value},
        }
    )


def stack(
    engine: Engine,
    server: MCPServer[Any],
    *,
    tasks: bool = True,
) -> tuple[RunController, ActionGateway, MCPActionCoordinator, OfficialSDKMCPClient]:
    configured = binding(tasks=tasks)
    client = OfficialSDKMCPClient(server, configured, subject_id=CONTEXT.subject_id)
    imported = MCPToolCatalog().import_tools(CONTEXT, configured, client)
    controller = RunController(engine)
    coordinator = MCPActionCoordinator(controller, {"official": (configured, client)})
    gateway = ActionGateway(
        engine,
        CapabilityRegistry(imported.capabilities),
        ResourceStore(engine),
        controller,
        integration_executor=coordinator,
    )
    return controller, gateway, coordinator, client


def submit_input(
    controller: RunController,
    input_request_id: str,
    values: dict[str, Any],
    *,
    key: str,
) -> None:
    request = controller.get_input_request(CONTEXT, input_request_id)
    controller.respond_to_input(
        CONTEXT,
        input_request_id,
        expected_version=request.version,
        decision="submit",
        values=values,
        comment=None,
        idempotency_key=key,
    )


def test_at_053_official_modern_discovery_call_and_http_header_body_consistency() -> None:
    server, _, _ = make_server()
    configured = binding()
    client = OfficialSDKMCPClient(server, configured, subject_id=CONTEXT.subject_id)

    discovered = client.discover()
    assert discovered.protocol_version == "2026-07-28"
    assert {tool.name for tool in discovered.tools} >= {"echo", "greet", "task_tool"}
    result = client.call_tool("echo", {"message": "hello"})
    assert result.structured_content == {
        "message": "hello",
        "nested": {"numbers": [1, 2, 3]},
    }

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def http_transport() -> AsyncIterator[Any]:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            follow_redirects=False,
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=http_client
            ) as streams:
                yield streams

    with TestClient(app) as http:
        wire_client = OfficialSDKMCPClient(
            http_transport(), configured, subject_id=CONTEXT.subject_id
        )
        assert wire_client.discover().protocol_version == "2026-07-28"
        wire_client = OfficialSDKMCPClient(
            http_transport(), configured, subject_id=CONTEXT.subject_id
        )
        wire_result = wire_client.call_tool("echo", {"message": "over-http"})
        assert wire_result.structured_content["message"] == "over-http"
        response = http.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "tools/call",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == HEADER_MISMATCH


def test_at_054_profiles_are_explicit_and_discovery_has_no_tool_side_effect() -> None:
    modern_server, _, modern_tasks = make_server()
    modern = OfficialSDKMCPClient(
        modern_server, binding(profile="modern2026-07-28"), subject_id=CONTEXT.subject_id
    )
    legacy_server, _, legacy_tasks = make_server()
    legacy = OfficialSDKMCPClient(
        legacy_server,
        binding(tasks=False, profile="legacy2025-11-25"),
        subject_id=CONTEXT.subject_id,
    )

    assert modern.discover().profile == "modern2026-07-28"
    assert legacy.discover().protocol_version == "2025-11-25"
    assert modern_tasks.created == legacy_tasks.created == 0


@pytest.mark.postgres
def test_at_055_complete_tool_error_protocol_error_and_arbitrary_json(
    clean_postgres: Engine,
) -> None:
    server, _, _ = make_server()
    _, gateway, _, _ = stack(clean_postgres, server)

    array = gateway.execute(
        CONTEXT,
        operation("array_result", {}),
        idempotency_key="array",
        transport_kind="mcp",
    )
    assert array.succeeded
    assert array.value["structured_content"] == [1, {"nested": True}]

    tool_error = gateway.execute(
        CONTEXT,
        operation("tool_error", {}),
        idempotency_key="tool-error",
        transport_kind="mcp",
    )
    assert not tool_error.succeeded
    assert tool_error.value["is_error"] is True

    with pytest.raises(HarnessError) as raised:
        gateway.execute(
            CONTEXT,
            operation("protocol_fault", {}),
            idempotency_key="protocol-error",
            transport_kind="mcp",
        )
    assert raised.value.code == "mcp_protocol_error"
    with Session(clean_postgres) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(MCPRPCRecord)
                .where(MCPRPCRecord.status == "protocol_error")
            )
            == 1
        )


@pytest.mark.postgres
def test_p06_lost_initial_mcp_response_blocks_without_resending(
    clean_postgres: Engine,
) -> None:
    server, _, _ = make_server()
    controller, gateway, _, _ = stack(clean_postgres, server)

    class LostResponseClient:
        calls = 0

        def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            self.calls += 1
            raise TimeoutError("response lost after possible effect")

    lost = LostResponseClient()
    gateway.integration_executor = MCPActionCoordinator(
        controller,
        {"official": (binding(), lost)},  # type: ignore[arg-type]
    )
    first = gateway.execute(
        CONTEXT,
        operation("echo", {"message": "maybe applied"}),
        idempotency_key="lost-response",
        transport_kind="mcp",
    )
    second = gateway.execute(
        CONTEXT,
        operation("echo", {"message": "maybe applied"}),
        idempotency_key="lost-response",
        transport_kind="mcp",
    )
    assert first.action_id == second.action_id
    assert lost.calls == 1
    action = controller.get_action(CONTEXT, first.action_id)
    assert action.status == ActionStatus.OUTCOME_UNKNOWN
    assert controller.get_run(CONTEXT, action.run_id).status == RunStatus.BLOCKED


@pytest.mark.postgres
def test_at_056_mrtr_restart_resumes_same_action_with_opaque_state_and_new_request_id(
    clean_postgres: Engine,
) -> None:
    server, observer, _ = make_server()
    controller, gateway, _, _ = stack(clean_postgres, server)
    pending = gateway.execute(
        CONTEXT,
        operation("greet", {}),
        idempotency_key="mrtr",
        transport_kind="mcp",
    )
    assert pending.status_code == 202
    replayed = gateway.execute(
        CONTEXT,
        operation("greet", {}),
        idempotency_key="mrtr",
        transport_kind="mcp",
    )
    assert replayed.action_id == pending.action_id
    assert replayed.value == pending.value
    input_request_id = str(pending.value["input_request_id"])
    submit_input(controller, input_request_id, {"name": "April"}, key="mrtr-response")

    restarted_controller, restarted_gateway, _, _ = stack(clean_postgres, server)
    completed = restarted_gateway.execute(
        CONTEXT,
        operation("greet", {}),
        idempotency_key="mrtr",
        transport_kind="mcp",
    )
    assert completed.succeeded
    assert "Hello April" in completed.value["content"][0]["text"]
    action = restarted_controller.get_action(CONTEXT, pending.action_id)
    assert action.status == ActionStatus.SUCCEEDED

    with Session(clean_postgres) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActionRecord)
                .where(ActionRecord.id == pending.action_id)
            )
            == 1
        )
        continuation = session.scalar(
            select(MCPContinuationRecord).where(
                MCPContinuationRecord.action_id == pending.action_id
            )
        )
        assert continuation is not None
        assert continuation.request_state
        assert continuation.status == "resumed"
        rpcs = session.scalars(
            select(MCPRPCRecord)
            .where(MCPRPCRecord.action_id == pending.action_id)
            .order_by(MCPRPCRecord.sequence)
        ).all()
        assert len(rpcs) == 2
        assert rpcs[0].correlation_id != rpcs[1].correlation_id
    tool_ids = [request_id for method, request_id in observer.requests if method == "tools/call"]
    assert len(tool_ids) == 2
    # The official in-process SDK starts a fresh protocol session for each call;
    # its numeric wire IDs may repeat across sessions, but these are distinct requests.
    assert all(request_id is not None for request_id in tool_ids)
    events = restarted_controller.get_event_history(CONTEXT, action.run_id, after=0, limit=100)
    assert continuation.request_state not in repr([event.data for event in events.events])


@pytest.mark.postgres
def test_at_057_task_handle_survives_restart_and_update_cancel_are_separate(
    clean_postgres: Engine,
) -> None:
    server, _, remote = make_server()
    _, gateway, _, _ = stack(clean_postgres, server)
    pending = gateway.execute(
        CONTEXT,
        operation("task_tool", {}),
        idempotency_key="task",
        transport_kind="mcp",
    )
    assert pending.status_code == 202
    assert remote.created == 1
    replayed = gateway.execute(
        CONTEXT,
        operation("task_tool", {}),
        idempotency_key="task",
        transport_kind="mcp",
    )
    assert replayed.action_id == pending.action_id
    assert remote.created == 1

    restarted_controller, restarted_gateway, restarted, _ = stack(clean_postgres, server)
    first_job = restarted_controller.claim_job("mcp-worker", kind="mcp_task_poll")
    assert first_job is not None
    needs_input = restarted.process_poll_job(CONTEXT, first_job, worker_id="mcp-worker")
    assert needs_input.value["result_type"] == "input_required"
    assert remote.created == 1
    submit_input(
        restarted_controller,
        str(needs_input.value["input_request_id"]),
        {"name": "April"},
        key="task-response",
    )
    update = restarted_gateway.execute(
        CONTEXT,
        operation("task_tool", {}),
        idempotency_key="task",
        transport_kind="mcp",
    )
    assert update.status_code == 202
    assert remote.updates == 1
    second_job = restarted_controller.claim_job("mcp-worker", kind="mcp_task_poll")
    assert second_job is not None
    completed = restarted.process_poll_job(CONTEXT, second_job, worker_id="mcp-worker")
    assert completed.succeeded
    assert completed.value["result"]["structuredContent"] == {"done": True}
    assert remote.created == 1

    second = gateway.execute(
        CONTEXT,
        operation("task_tool", {}),
        idempotency_key="task-cancel",
        transport_kind="mcp",
    )
    cancel_ack = restarted.cancel_task(CONTEXT, second.action_id)
    assert cancel_ack.value["cancel_acknowledged"] is True
    assert cancel_ack.value["stopped"] is False
    assert restarted_controller.get_action(CONTEXT, second.action_id).status == (
        ActionStatus.WAITING_EXTERNAL
    )
    cancelled = restarted.poll_task(CONTEXT, second.action_id)
    assert cancelled.value["status"] == "cancelled"
    assert remote.cancels == 1
    with Session(clean_postgres) as session:
        rows = session.scalars(select(MCPTaskRecord).order_by(MCPTaskRecord.task_id)).all()
        assert [row.status for row in rows] == ["completed", "cancelled"]


@pytest.mark.postgres
def test_p06_initially_completed_task_commits_action_once(clean_postgres: Engine) -> None:
    server, _, remote = make_server()
    controller, gateway, _, _ = stack(clean_postgres, server)
    completed = gateway.execute(
        CONTEXT,
        operation("task_immediate", {}),
        idempotency_key="immediate-task",
        transport_kind="mcp",
    )
    assert completed.succeeded
    assert completed.value["status"] == "completed"
    assert controller.get_action(CONTEXT, completed.action_id).status == ActionStatus.SUCCEEDED
    assert remote.created == 1
    replay = gateway.execute(
        CONTEXT,
        operation("task_immediate", {}),
        idempotency_key="immediate-task",
        transport_kind="mcp",
    )
    assert replay.action_id == completed.action_id
    assert remote.created == 1


@pytest.mark.postgres
def test_p06_stale_poll_worker_is_fenced_before_remote_request(clean_postgres: Engine) -> None:
    server, _, remote = make_server()
    controller, gateway, coordinator, _ = stack(clean_postgres, server)
    gateway.execute(
        CONTEXT,
        operation("task_tool", {}),
        idempotency_key="stale-poll",
        transport_kind="mcp",
    )
    stale = controller.claim_job("old-worker", kind="mcp_task_poll")
    assert stale is not None
    with Session(clean_postgres) as session:
        with session.begin():
            job = session.get(JobRecord, stale.id)
            assert job is not None
            job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    current = controller.claim_job("new-worker", kind="mcp_task_poll")
    assert current is not None
    assert current.id == stale.id
    assert current.lease_epoch > stale.lease_epoch
    with pytest.raises(LeaseLost):
        coordinator.process_poll_job(CONTEXT, stale, worker_id="old-worker")
    assert remote.gets == 0
    coordinator.process_poll_job(CONTEXT, current, worker_id="new-worker")
    assert remote.gets == 1


@pytest.mark.postgres
def test_at_058_extensions_are_advertised_only_when_enabled(clean_postgres: Engine) -> None:
    server, _, _ = make_server()
    disabled_binding = binding(tasks=False)
    disabled_client = OfficialSDKMCPClient(server, disabled_binding, subject_id=CONTEXT.subject_id)
    imported = MCPToolCatalog().import_tools(CONTEXT, disabled_binding, disabled_client)
    task_capability = next(item for item in imported.capabilities if item.id.endswith("task_tool"))
    assert "task" not in task_capability.supported_result_modes
    _, gateway, _, _ = stack(clean_postgres, server, tasks=False)
    with pytest.raises(HarnessError) as raised:
        gateway.execute(
            CONTEXT,
            operation("task_tool", {}),
            idempotency_key="unsupported-task",
            transport_kind="mcp",
        )
    assert raised.value.code == "mcp_protocol_error"
    with Session(clean_postgres) as session:
        action = session.scalar(
            select(ActionRecord).where(ActionRecord.capability_id == "mcp.official.task_tool")
        )
        assert action is not None
        assert action.status == ActionStatus.FAILED.value


def test_at_059_credential_binding_isolated_by_issuer_resource_and_subject() -> None:
    vault = InMemoryCredentialVault(
        (
            CredentialBinding(
                "credential-1",
                "official",
                "https://issuer.example",
                "https://mcp.example",
                CONTEXT.subject_id,
                "upstream-only-token",
                tenant_id=CONTEXT.tenant_id,
            ),
        )
    )
    configured = MCPIntegrationBinding(
        integration_id="official",
        profile="modern2026-07-28",
        credential_reference="credential-1",
        issuer="https://issuer.example",
        resource="https://mcp.example",
    )
    client = OfficialSDKMCPClient(
        "https://mcp.example/mcp",
        configured,
        subject_id=CONTEXT.subject_id,
        tenant_id=CONTEXT.tenant_id,
        credential_vault=vault,
    )
    transport = client._server_or_transport()
    assert transport is not None
    assert "upstream-only-token" not in repr(client)
    wrong_endpoint = OfficialSDKMCPClient(
        "https://other-mcp.example/mcp",
        configured,
        subject_id=CONTEXT.subject_id,
        tenant_id=CONTEXT.tenant_id,
        credential_vault=vault,
    )
    with pytest.raises(InvalidOperation):
        wrong_endpoint._server_or_transport()
    with pytest.raises(PermissionDenied):
        vault.resolve(
            "credential-1",
            integration_id="official",
            tenant_id=CONTEXT.tenant_id,
            issuer="https://other-issuer.example",
            resource="https://mcp.example",
            subject_id=CONTEXT.subject_id,
        )
    with pytest.raises(PermissionDenied):
        vault.resolve(
            "credential-1",
            integration_id="official",
            tenant_id=CONTEXT.tenant_id,
            issuer="https://issuer.example",
            resource="https://other-mcp.example",
            subject_id=CONTEXT.subject_id,
        )
    with pytest.raises(PermissionDenied):
        vault.resolve(
            "credential-1",
            integration_id="official",
            tenant_id="other-tenant",
            issuer="https://issuer.example",
            resource="https://mcp.example",
            subject_id=CONTEXT.subject_id,
        )
    wrong_tenant = OfficialSDKMCPClient(
        "https://mcp.example/mcp",
        configured,
        subject_id=CONTEXT.subject_id,
        tenant_id="other-tenant",
        credential_vault=vault,
    )
    with pytest.raises(PermissionDenied):
        wrong_tenant._server_or_transport()


def test_p06_http_transport_forwards_only_the_bound_upstream_token() -> None:
    first_server, _, _ = make_server()
    second_server, _, _ = make_server()
    applications = [
        first_server.streamable_http_app(
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ),
        second_server.streamable_http_app(
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ),
    ]
    vault = InMemoryCredentialVault(
        (
            CredentialBinding(
                "credential-a",
                "official-a",
                "https://issuer-a.example",
                "https://mcp-a.example",
                CONTEXT.subject_id,
                "upstream-a-only",
                tenant_id=CONTEXT.tenant_id,
            ),
            CredentialBinding(
                "credential-b",
                "official-b",
                "https://issuer-b.example",
                "https://mcp-b.example",
                CONTEXT.subject_id,
                "upstream-b-only",
                tenant_id=CONTEXT.tenant_id,
            ),
        )
    )
    observed: list[list[str | None]] = [[], []]

    def make_transport(index: int) -> Any:
        async def recorder(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                authorization = headers.get(b"authorization")
                observed[index].append(
                    None if authorization is None else authorization.decode("ascii")
                )
            await applications[index](scope, receive, send)

        @asynccontextmanager
        async def transport(url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=recorder),
                headers=headers,
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                async with streamable_http_client(url, http_client=http_client) as streams:
                    yield streams

        return transport

    for index, suffix in enumerate(("a", "b")):
        configured = MCPIntegrationBinding(
            integration_id=f"official-{suffix}",
            profile="modern2026-07-28",
            credential_reference=f"credential-{suffix}",
            issuer=f"https://issuer-{suffix}.example",
            resource=f"https://mcp-{suffix}.example",
        )
        with TestClient(applications[index]):
            client = OfficialSDKMCPClient(
                f"https://mcp-{suffix}.example/mcp",
                configured,
                subject_id=CONTEXT.subject_id,
                tenant_id=CONTEXT.tenant_id,
                credential_vault=vault,
                http_transport_factory=make_transport(index),
            )
            assert client.discover().protocol_version == "2026-07-28"
    assert observed[0] and set(observed[0]) == {"Bearer upstream-a-only"}
    assert observed[1] and set(observed[1]) == {"Bearer upstream-b-only"}
    assert "harness-user-token" not in repr(observed)


def test_p06_issuer_pinned_oauth_sdk_can_discover_official_mcp_server() -> None:
    server, _, _ = make_server()
    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    resource = "https://resource.example.test/mcp"
    issuer = "https://issuer.example.test"
    configured = MCPIntegrationBinding(
        integration_id="oauth-official",
        profile="modern2026-07-28",
        credential_reference="oauth-credential",
        issuer=issuer,
        resource=resource,
    )
    vault = InMemoryOAuthClientVault(
        (
            OAuthClientCredentialBinding(
                "oauth-credential",
                "oauth-official",
                CONTEXT.tenant_id,
                issuer,
                resource,
                CONTEXT.subject_id,
                "client-one",
                "client-secret-one",
            ),
        )
    )
    observed: list[tuple[str, str | None]] = []
    asgi = httpx2.ASGITransport(app=app)

    async def handle(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        authorization = request.headers.get("Authorization")
        observed.append((url, authorization))
        if url == resource:
            if authorization != "Bearer oauth-access":
                return httpx2.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://resource.example.test/'
                            '.well-known/oauth-protected-resource/mcp"'
                        )
                    },
                )
            return await asgi.handle_async_request(request)
        if url == "https://resource.example.test/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={"resource": resource, "authorization_servers": [issuer]},
            )
        if url == f"{issuer}/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["client_credentials"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        if url == f"{issuer}/token":
            return httpx2.Response(
                200,
                json={"access_token": "oauth-access", "token_type": "Bearer", "expires_in": 3600},
            )
        return httpx2.Response(404)

    transport = IssuerPinnedOAuthMCPTransport(
        configured,
        CONTEXT,
        vault,
        InMemoryOAuthTokenStorage(),
        transport=httpx2.MockTransport(handle),
    )
    with TestClient(app):
        client = OfficialSDKMCPClient(
            resource,
            configured,
            subject_id=CONTEXT.subject_id,
            tenant_id=CONTEXT.tenant_id,
            oauth_transport_factory=transport,
        )
        assert client.discover().protocol_version == "2026-07-28"
    assert any(url == f"{issuer}/token" for url, _authorization in observed)
    assert any(url == resource and auth == "Bearer oauth-access" for url, auth in observed)
    assert all(
        authorization is None or url in {resource, f"{issuer}/token"}
        for url, authorization in observed
    )


def test_p06_expired_or_revoked_credentials_fail_before_transport_creation() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CredentialBinding(
            "naive",
            "official",
            "https://issuer.example",
            "https://mcp.example",
            CONTEXT.subject_id,
            "invalid-token",
            tenant_id=CONTEXT.tenant_id,
            expires_at=datetime(2026, 9, 22),
        )
    expired = CredentialBinding(
        "expired",
        "official",
        "https://issuer.example",
        "https://mcp.example",
        CONTEXT.subject_id,
        "expired-token",
        tenant_id=CONTEXT.tenant_id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    active = CredentialBinding(
        "active",
        "official",
        "https://issuer.example",
        "https://mcp.example",
        CONTEXT.subject_id,
        "active-token",
        tenant_id=CONTEXT.tenant_id,
    )
    vault = InMemoryCredentialVault((expired, active))
    calls = 0

    @asynccontextmanager
    async def unexpected_transport(_url: str, _headers: dict[str, str]) -> AsyncIterator[Any]:
        nonlocal calls
        calls += 1
        yield None

    for reference in ("expired", "active"):
        if reference == "active":
            vault.revoke(reference)
        configured = MCPIntegrationBinding(
            integration_id="official",
            profile="modern2026-07-28",
            credential_reference=reference,
            issuer="https://issuer.example",
            resource="https://mcp.example",
        )
        client = OfficialSDKMCPClient(
            "https://mcp.example/mcp",
            configured,
            subject_id=CONTEXT.subject_id,
            tenant_id=CONTEXT.tenant_id,
            credential_vault=vault,
            http_transport_factory=unexpected_transport,
        )
        with pytest.raises(PermissionDenied):
            client._server_or_transport()
    assert calls == 0


def test_p06_catalog_cache_is_private_to_subject_scope_and_policy_revision() -> None:
    class DiscoveryClient:
        calls = 0

        def discover(self) -> MCPDiscovery:
            self.calls += 1
            return MCPDiscovery(
                protocol_version="2026-07-28",
                profile="modern2026-07-28",
                tools=(
                    MCPToolDescriptor(
                        name="echo",
                        description="Echo",
                        input_schema={"type": "object"},
                        output_schema=None,
                    ),
                ),
                ttl_ms=60000,
                cache_scope="public",
            )

    catalog = MCPToolCatalog()
    client = DiscoveryClient()
    configured = binding()
    catalog.import_tools(CONTEXT, configured, client)  # type: ignore[arg-type]
    catalog.import_tools(CONTEXT, configured, client)  # type: ignore[arg-type]
    assert client.calls == 1
    other_subject = TrustedContext(CONTEXT.tenant_id, "other-subject", CONTEXT.scopes)
    catalog.import_tools(other_subject, configured, client)  # type: ignore[arg-type]
    other_tenant = TrustedContext("other-tenant", CONTEXT.subject_id, CONTEXT.scopes)
    catalog.import_tools(other_tenant, configured, client)  # type: ignore[arg-type]
    narrower = TrustedContext(CONTEXT.tenant_id, CONTEXT.subject_id, frozenset())
    catalog.import_tools(narrower, configured, client)  # type: ignore[arg-type]
    revised = MCPIntegrationBinding(
        integration_id=configured.integration_id,
        profile=configured.profile,
        policy_revision="dev-2",
    )
    catalog.import_tools(CONTEXT, revised, client)  # type: ignore[arg-type]
    assert client.calls == 5
    assert len(catalog.cache_keys) == 5


def test_p06_mcp_http_transport_does_not_forward_token_across_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str | None]] = []

    async def redirect_app(scope: Any, receive: Any, send: Any) -> None:
        headers = dict(scope["headers"])
        observed.append(
            (
                headers[b"host"].decode("ascii"),
                None
                if b"authorization" not in headers
                else headers[b"authorization"].decode("ascii"),
            )
        )
        await RedirectResponse("https://other-mcp.example/mcp", status_code=307)(
            scope, receive, send
        )

    original_client = httpx2.AsyncClient

    def local_client(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return original_client(*args, transport=httpx2.ASGITransport(app=redirect_app), **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", local_client)
    vault = InMemoryCredentialVault(
        (
            CredentialBinding(
                "credential-redirect",
                "official",
                "https://issuer.example",
                "https://mcp.example",
                CONTEXT.subject_id,
                "only-original-origin",
                tenant_id=CONTEXT.tenant_id,
            ),
        )
    )
    configured = MCPIntegrationBinding(
        integration_id="official",
        profile="modern2026-07-28",
        credential_reference="credential-redirect",
        issuer="https://issuer.example",
        resource="https://mcp.example",
    )
    client = OfficialSDKMCPClient(
        "https://mcp.example/mcp",
        configured,
        subject_id=CONTEXT.subject_id,
        tenant_id=CONTEXT.tenant_id,
        credential_vault=vault,
    )
    with pytest.raises((MCPError, ExceptionGroup, httpx2.HTTPStatusError)):
        client.discover()
    assert observed
    assert all(host == "mcp.example" for host, _ in observed)
    assert all(token == "Bearer only-original-origin" for _, token in observed)


@pytest.mark.postgres
def test_p06_trusted_request_scoped_http_composition_keeps_catalog_and_action_aligned(
    clean_postgres: Engine,
) -> None:
    server, observer, _ = make_server()
    seen_subjects: list[tuple[str, str]] = []

    def client_factory(
        configured: MCPIntegrationBinding, context: TrustedContext
    ) -> OfficialSDKMCPClient:
        seen_subjects.append((context.tenant_id, context.subject_id))
        return OfficialSDKMCPClient(
            server,
            configured,
            subject_id=context.subject_id,
            tenant_id=context.tenant_id,
        )

    manager = MCPIntegrationManager((binding(),), client_factory)
    authenticator = DevelopmentAuthenticator(
        {
            "authorized": DevelopmentPrincipal(
                CONTEXT.tenant_id,
                CONTEXT.subject_id,
                CONTEXT.scopes | {"capabilities:read"},
            ),
            "catalog-only": DevelopmentPrincipal(
                CONTEXT.tenant_id,
                "catalog-only-subject",
                frozenset({"capabilities:read"}),
            ),
        }
    )
    app = create_app(
        engine=clean_postgres,
        authenticator=authenticator,
        mcp_integrations=manager,
    )
    with TestClient(app) as http:
        listed = http.get(
            "/v1/capabilities?limit=100",
            headers={"Authorization": "Bearer authorized"},
        )
        assert listed.status_code == 200
        assert any(item["id"] == "mcp.official.echo" for item in listed.json()["items"])
        hidden = http.get(
            "/v1/capabilities?limit=100",
            headers={"Authorization": "Bearer catalog-only"},
        )
        assert hidden.status_code == 200
        assert all(not item["id"].startswith("mcp.") for item in hidden.json()["items"])
        denied = http.post(
            "/v1/integrations/official/operations/echo/invocations",
            headers={"Authorization": "Bearer catalog-only", "Idempotency-Key": "denied"},
            json={"message": "no"},
        )
        assert denied.status_code == 403
        accepted = http.post(
            "/v1/integrations/official/operations/echo/invocations",
            headers={"Authorization": "Bearer authorized", "Idempotency-Key": "authorized"},
            json={"message": "hello"},
        )
        assert accepted.status_code == 202
        action_id = accepted.json()["resource_id"]
        assert RunController(clean_postgres).get_action(CONTEXT, action_id).status == (
            ActionStatus.SUCCEEDED
        )
        replay = http.post(
            "/v1/integrations/official/operations/echo/invocations",
            headers={"Authorization": "Bearer authorized", "Idempotency-Key": "authorized"},
            json={"message": "hello"},
        )
        assert replay.status_code == 202
        assert replay.json()["resource_id"] == action_id
    assert seen_subjects and set(seen_subjects) == {(CONTEXT.tenant_id, CONTEXT.subject_id)}
    assert [method for method, _ in observer.requests].count("tools/call") == 1
