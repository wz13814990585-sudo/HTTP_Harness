from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hnh.adapters.models.function_tools import FunctionToolCatalog
from hnh.adapters.models.openai_responses import OpenAIFunctionToolsProvider
from hnh.application.capabilities import builtin_capabilities
from hnh.domain.errors import ModelOutputInvalid
from hnh.ports.models import ContextSnapshot, ModelLimits, ModelTurn


def _catalog() -> FunctionToolCatalog:
    return FunctionToolCatalog([item.public() for item in builtin_capabilities()])


def _name(catalog: FunctionToolCatalog, description: str) -> str:
    return next(item["name"] for item in catalog.tools if item["description"] == description)


def test_native_function_catalog_decodes_to_same_bound_http_shape() -> None:
    catalog = _catalog()
    write = _name(catalog, "Conditionally write a versioned managed workspace file.")
    operation = catalog.decode(
        write,
        json.dumps(
            {
                "workspace_id": "ws",
                "file_path": "report/final.txt",
                "text": "ready",
                "if_none_match": "*",
            }
        ),
    )
    assert operation.method == "PUT"
    assert operation.target == "/v1/workspaces/ws/files/report/final.txt"
    assert operation.headers == {"content-type": "text/plain", "if-none-match": "*"}
    assert operation.payload == {"kind": "text", "text": "ready"}

    read = _name(catalog, "Read a versioned managed workspace file.")
    assert (
        catalog.decode(read, '{"workspace_id":"ws","file_path":"report/final.txt"}').target
        == "/v1/workspaces/ws/files/report/final.txt"
    )
    echo = _name(catalog, "Deterministic local echo used to verify adapter-independent dispatch.")
    assert catalog.decode(echo, '{"message":"hello"}').payload == {
        "kind": "json",
        "value": {"message": "hello"},
    }


@pytest.mark.parametrize(
    ("name_kind", "arguments"),
    [
        ("unknown", '{"message":"hello"}'),
        ("echo", '{"message":"hello","Authorization":"forged"}'),
        ("echo", '{"message":"hello"'),
        ("read", '{"workspace_id":"ws/evil","file_path":"x"}'),
        ("write", '{"workspace_id":"ws","file_path":"x","text":"x"}'),
    ],
)
def test_native_function_catalog_rejects_unknown_or_invalid_proposals(
    name_kind: str, arguments: str
) -> None:
    catalog = _catalog()
    names = {
        "unknown": "forged_admin_tool",
        "echo": _name(
            catalog, "Deterministic local echo used to verify adapter-independent dispatch."
        ),
        "read": _name(catalog, "Read a versioned managed workspace file."),
        "write": _name(catalog, "Conditionally write a versioned managed workspace file."),
    }
    with pytest.raises(ModelOutputInvalid):
        catalog.decode(names[name_kind], arguments)


def test_openai_function_provider_sends_real_function_tools_and_preserves_call() -> None:
    captured: dict[str, Any] = {}
    catalog = _catalog()
    echo = _name(catalog, "Deterministic local echo used to verify adapter-independent dispatch.")

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_func",
                "model": "gpt-test",
                "status": "completed",
                "output": [
                    {
                        "id": "fc_1",
                        "call_id": "call_1",
                        "type": "function_call",
                        "name": echo,
                        "arguments": '{"message":"hello"}',
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 8},
            },
        )

    provider = OpenAIFunctionToolsProvider(
        api_key="test-key",
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = provider.generate(
        ContextSnapshot(
            "ctx",
            "run",
            1,
            {"goal": "echo", "capabilities": [{"path_template": "/v1/x"}]},
            (),
            "a" * 64,
        ),
        [item.public() for item in builtin_capabilities()],
        ModelLimits(max_output_tokens=512),
    )
    body = captured["body"]
    assert body["stream"] is False and body["store"] is False
    assert body["tools"] == catalog.tools
    assert "capabilities" not in json.loads(body["input"])["context"]
    assert body["parallel_tool_calls"] is True
    assert body["tools"][0]["type"] == "function"
    assert "operations" not in body["text"]["format"]["schema"]["properties"]
    assert result.raw_response["output"][0]["call_id"] == "call_1"
    turn = ModelTurn.model_validate(result.output)
    assert len(turn.operations) == 1
    assert turn.operations[0].target == "/v1/integrations/native/operations/echo/invocations"
    assert result.usage["input_tokens"] == 5


def test_openai_function_provider_accepts_final_candidate_without_calls() -> None:
    final = {
        "public_output": "done",
        "final_candidate": {
            "answer": "Done",
            "artifact_ids": ["artifact_1"],
            "evidence_ids": ["evt_1"],
            "acceptance_claims": ["verified"],
        },
        "request_input": None,
    }

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_final",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(final)}],
                    }
                ],
            },
        )

    provider = OpenAIFunctionToolsProvider(
        api_key="test-key",
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = provider.generate(
        ContextSnapshot("ctx", "run", 1, {"goal": "done"}, (), "a" * 64),
        [],
        ModelLimits(max_output_tokens=512),
    )
    assert result.output["operations"] == []
    assert ModelTurn.model_validate(result.output).final_candidate is not None


def test_openai_function_provider_decodes_all_completed_calls_without_executing() -> None:
    catalog = _catalog()
    echo = _name(catalog, "Deterministic local echo used to verify adapter-independent dispatch.")

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_multi",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": f"call_{index}",
                        "name": echo,
                        "arguments": json.dumps({"message": f"hello-{index}"}),
                    }
                    for index in (1, 2)
                ],
            },
        )

    provider = OpenAIFunctionToolsProvider(
        api_key="test-key",
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = provider.generate(
        ContextSnapshot("ctx", "run", 1, {"goal": "echo twice"}, (), "a" * 64),
        [item.public() for item in builtin_capabilities()],
        ModelLimits(max_output_tokens=512),
    )
    turn = ModelTurn.model_validate(result.output)
    assert [operation.payload for operation in turn.operations] == [
        {"kind": "json", "value": {"message": "hello-1"}},
        {"kind": "json", "value": {"message": "hello-2"}},
    ]
    assert len(result.raw_response["output"]) == 2


def test_openai_function_provider_rejects_duplicate_call_ids() -> None:
    catalog = _catalog()
    echo = _name(catalog, "Deterministic local echo used to verify adapter-independent dispatch.")

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_duplicate",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "same-call-id",
                        "name": echo,
                        "arguments": '{"message":"hello"}',
                    }
                    for _ in range(2)
                ],
            },
        )

    provider = OpenAIFunctionToolsProvider(
        api_key="test-key",
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ModelOutputInvalid):
        provider.generate(
            ContextSnapshot("ctx", "run", 1, {"goal": "echo"}, (), "a" * 64),
            [item.public() for item in builtin_capabilities()],
            ModelLimits(max_output_tokens=512),
        )
