"""Four-arm wiring check with mocked model responses, never a live benchmark."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine

from hnh.adapters.mcp.sdk import OfficialSDKMCPClient
from hnh.adapters.models.function_tools import FunctionToolCatalog
from hnh.adapters.models.openai_responses import (
    OpenAIFunctionToolsProvider,
    OpenAIResponsesProvider,
)
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.mcp_catalog import MCPIntegrationBinding
from hnh.application.mcp_composition import MCPIntegrationManager
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.runner import Runner
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus

CONTEXT = TrustedContext(
    "tenant-p08-arms", "subject-p08-arms", frozenset({"runs:read", "integrations:invoke"})
)


def _mcp_manager() -> MCPIntegrationManager:
    server: MCPServer[Any] = MCPServer("p08-comparable-echo", version="1.0.0")

    @server.tool(structured_output=True)
    def echo(message: str) -> dict[str, str]:
        """Deterministic local echo used to verify adapter-independent dispatch."""
        return {"message": message}

    binding = MCPIntegrationBinding(
        integration_id="eval",
        profile="modern2026-07-28",
        tool_effects={"echo": "read_only"},
    )

    def factory(configured: MCPIntegrationBinding, context: TrustedContext) -> OfficialSDKMCPClient:
        return OfficialSDKMCPClient(
            server,
            configured,
            subject_id=context.subject_id,
            tenant_id=context.tenant_id,
        )

    return MCPIntegrationManager((binding,), factory)


def _response_for_arm(
    surface: str,
    downstream: str,
    index: int,
    function_name: str,
    operation: dict[str, Any],
    model_inputs: dict[tuple[str, str], dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model_inputs[(surface, downstream)] = body
        output: list[dict[str, Any]]
        if surface == "native_function_schema":
            assert body["tools"][index]["name"] == function_name
            output = [
                {
                    "type": "function_call",
                    "call_id": f"call-{surface}-{downstream}",
                    "name": function_name,
                    "arguments": '{"message":"same-input"}',
                }
            ]
        else:
            assert "tools" not in body
            turn = {
                "public_output": "",
                "operations": [operation],
                "final_candidate": None,
                "request_input": None,
            }
            output = [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(turn)}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"resp-{surface}-{downstream}",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    return respond


@pytest.mark.postgres
def test_four_controlled_arms_reach_same_gateway_and_echo_semantics(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    mcp_manager = _mcp_manager()
    outcomes: dict[tuple[str, str], dict[str, Any]] = {}
    model_inputs: dict[tuple[str, str], dict[str, Any]] = {}
    for surface in ("native_function_schema", "http_semantic_operation"):
        for downstream in ("native_http", "mcp_adapter"):
            registry, integration_executor = (
                (CapabilityRegistry(), None)
                if downstream == "native_http"
                else mcp_manager.for_context(CONTEXT, controller)
            )
            capability_id = "native.echo" if downstream == "native_http" else "mcp.eval.echo"
            capability = registry.get(CONTEXT, capability_id)
            operation = {
                "method": capability.method,
                "target": capability.path_template,
                "headers": {"content-type": "application/json"},
                "payload": {"kind": "json", "value": {"message": "same-input"}},
            }
            visible = [item.public() for item in registry.list(CONTEXT, None, 100).items]
            index = next(i for i, item in enumerate(visible) if item["id"] == capability_id)
            function_name = FunctionToolCatalog(visible).tools[index]["name"]
            arm = (surface, downstream)

            provider_type = (
                OpenAIFunctionToolsProvider
                if surface == "native_function_schema"
                else OpenAIResponsesProvider
            )
            provider = provider_type(
                api_key="mock-only",
                model="controlled-model",
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        _response_for_arm(
                            surface, downstream, index, function_name, operation, model_inputs
                        )
                    )
                ),
            )
            resources = ResourceStore(clean_postgres)
            gateway = ActionGateway(
                clean_postgres,
                registry,
                resources,
                controller,
                integration_executor=integration_executor,
            )
            runner = Runner(
                controller,
                ContextBuilder(controller, registry),
                gateway,
                CompletionGate(clean_postgres, controller),
                provider,
            )
            run_id = controller.admit_run(
                CONTEXT,
                {
                    "agent_id": "p08-eval",
                    "input": "Echo same-input once and report the observation.",
                    "limits": {
                        "max_model_turns": 2,
                        "max_tool_calls": 2,
                        "max_output_tokens": 1024,
                    },
                },
                f"controlled-{surface}-{downstream}",
            ).resource_id
            result = runner.advance(CONTEXT, run_id)
            assert result.status == RunStatus.RUNNING
            actions = controller.list_actions(CONTEXT, run_id)
            assert len(actions) == 1
            assert actions[0].status == ActionStatus.SUCCEEDED
            assert actions[0].result is not None
            value = actions[0].result["value"]
            outcomes[arm] = value if downstream == "native_http" else value["structured_content"]

    assert len(outcomes) == 4
    assert all(value == {"message": "same-input"} for value in outcomes.values())
    assert {json.loads(body["input"])["context"]["goal"] for body in model_inputs.values()} == {
        "Echo same-input once and report the observation."
    }
    assert {body["model"] for body in model_inputs.values()} == {"controlled-model"}
    assert {body["max_output_tokens"] for body in model_inputs.values()} == {1024}
