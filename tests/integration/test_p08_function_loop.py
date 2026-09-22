from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from hnh.adapters.models.function_tools import FunctionToolCatalog
from hnh.adapters.models.openai_responses import OpenAIFunctionToolsProvider
from hnh.adapters.postgres.models import ActionRecord, ModelCallRecord
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.runner import Runner, RunnerHooks
from hnh.domain.identity import TrustedContext
from hnh.domain.states import RunStatus

CONTEXT = TrustedContext(
    "tenant-p08-function",
    "subject-p08-function",
    frozenset({"runs:read", "workspace:write", "workspace:read"}),
)


@pytest.mark.postgres
def test_native_function_call_is_durable_before_common_gateway_dispatch(
    clean_postgres: Engine,
) -> None:
    registry = CapabilityRegistry()
    catalog = FunctionToolCatalog(
        [item.public() for item in registry.list(CONTEXT, None, 100).items]
    )
    write = next(
        item["name"]
        for item in catalog.tools
        if item["description"] == "Conditionally write a versioned managed workspace file."
    )

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"] == catalog.tools
        return httpx.Response(
            200,
            json={
                "id": "resp_p08_function",
                "status": "completed",
                "output": [
                    {
                        "id": "fc_p08",
                        "type": "function_call",
                        "call_id": "call_p08",
                        "name": write,
                        "arguments": json.dumps(
                            {
                                "workspace_id": "ws-p08",
                                "file_path": "native.txt",
                                "text": "committed before execution",
                                "if_none_match": "*",
                            }
                        ),
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 8},
            },
        )

    controller = RunController(clean_postgres)
    resources = ResourceStore(clean_postgres)
    gateway = ActionGateway(clean_postgres, registry, resources, controller)
    provider = OpenAIFunctionToolsProvider(
        api_key="local-mock-key",
        model="mock-model",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
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
            "agent_id": "agent-p08",
            "input": "write native.txt",
            "workspace_id": "ws-p08",
            "limits": {"max_model_turns": 2, "max_tool_calls": 2, "max_output_tokens": 2048},
        },
        "p08-function-run",
    ).resource_id

    def before_persist() -> None:
        with Session(clean_postgres) as session:
            call = session.scalar(select(ModelCallRecord).where(ModelCallRecord.run_id == run_id))
            assert call is not None and call.response_payload is None
            assert session.scalar(select(ActionRecord).where(ActionRecord.run_id == run_id)) is None

    def after_persist() -> None:
        with Session(clean_postgres) as session:
            call = session.scalar(select(ModelCallRecord).where(ModelCallRecord.run_id == run_id))
            assert call is not None
            assert call.response_payload["output"][0]["call_id"] == "call_p08"
            assert session.scalar(select(ActionRecord).where(ActionRecord.run_id == run_id)) is None

    result = runner.advance(
        CONTEXT,
        run_id,
        hooks=RunnerHooks(
            after_provider_before_persist=before_persist,
            after_persist_before_actions=after_persist,
        ),
    )
    assert result.status == RunStatus.RUNNING
    assert resources.read_file(CONTEXT, "ws-p08", "native.txt").content == (
        b"committed before execution"
    )
    with Session(clean_postgres) as session:
        actions = session.scalars(select(ActionRecord).where(ActionRecord.run_id == run_id)).all()
        assert len(actions) == 1
        assert actions[0].capability_id == "workspace.file.write"
