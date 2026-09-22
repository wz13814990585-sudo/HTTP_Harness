from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hnh.adapters.models.deepseek_responses import (
    DeepSeekFunctionToolsProvider,
    DeepSeekResponsesProvider,
)
from hnh.adapters.models.openai_responses import (
    OpenAIFunctionToolsProvider,
    OpenAIResponsesProvider,
)
from hnh.domain.errors import ModelOutputInvalid
from hnh.ports.models import ContextSnapshot, ModelLimits


def test_deepseek_responses_provider_sends_reasoning_without_persisting_it() -> None:
    captured: dict[str, Any] = {}
    decision = {
        "public_output": "need input",
        "operations": [],
        "final_candidate": None,
        "request_input": "Which file?",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_deepseek",
                "model": "deepseek-flash",
                "status": "completed",
                "output": [
                    {
                        "id": "reasoning_private",
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "private reasoning"}],
                    },
                    {
                        "id": "message_public",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(decision)}],
                    },
                ],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 11,
                    "total_tokens": 18,
                    "output_tokens_details": {"reasoning_tokens": 5},
                },
            },
        )

    provider = DeepSeekResponsesProvider(
        api_key="deepseek-test-secret",
        model="deepseek-flash",
        base_url="https://deepseek.example.test",
        reasoning_effort="max",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    response = provider.generate(
        ContextSnapshot("ctx", "run", 1, {"goal": "inspect"}, (), "a" * 64),
        [],
        ModelLimits(max_output_tokens=512),
    )

    assert captured["url"] == "https://deepseek.example.test/responses"
    assert captured["authorization"] == "Bearer deepseek-test-secret"
    assert captured["body"]["reasoning"] == {"effort": "max"}
    assert captured["body"]["text"]["format"]["type"] == "json_schema"
    assert provider.name == "deepseek-responses"
    assert response.output == decision
    assert response.raw_response["output"] == [
        {
            "id": "message_public",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(decision)}],
        }
    ]
    assert "private reasoning" not in json.dumps(response.raw_response)
    assert "deepseek-test-secret" not in json.dumps(response.raw_response)


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "unknown"])
def test_deepseek_provider_rejects_unverified_or_retired_model_ids(model: str) -> None:
    with pytest.raises(ValueError, match="unsupported DeepSeek model"):
        DeepSeekResponsesProvider(api_key="test", model=model)


def test_deepseek_provider_rejects_unknown_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="unsupported DeepSeek reasoning effort"):
        DeepSeekResponsesProvider(api_key="test", model="deepseek-flash", reasoning_effort="ultra")


def test_deepseek_function_provider_decodes_calls_through_common_codec() -> None:
    captured: dict[str, Any] = {}
    capability = {
        "id": "native.echo",
        "description": "Echo a message.",
        "method": "POST",
        "path_template": "/v1/integrations/native/operations/echo/invocations",
        "request_media_types": ["application/json"],
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    }

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "resp_deepseek_function",
                "model": "deepseek-flash",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "private"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_echo",
                        "name": body["tools"][0]["name"],
                        "arguments": json.dumps({"message": "hello"}),
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            },
        )

    provider = DeepSeekFunctionToolsProvider(
        api_key="test",
        model="deepseek-flash",
        reasoning_effort="low",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    response = provider.generate(
        ContextSnapshot("ctx", "run", 1, {"goal": "echo"}, (), "b" * 64),
        [capability],
        ModelLimits(max_output_tokens=256),
    )

    assert provider.name == "deepseek-function-tools"
    assert captured["body"]["reasoning"] == {"effort": "low"}
    assert response.output["operations"] == [
        {
            "method": "POST",
            "target": "/v1/integrations/native/operations/echo/invocations",
            "query": [],
            "headers": {"content-type": "application/json"},
            "payload": {"kind": "json", "value": {"message": "hello"}},
        }
    ]
    assert all(item["type"] != "reasoning" for item in response.raw_response["output"])


def test_openai_responses_provider_uses_structured_nonstreaming_request() -> None:
    captured: dict[str, Any] = {}
    output = {
        "public_output": "inspect the file",
        "operations": [
            {
                "method": "GET",
                "target": "/v1/workspaces/ws/files/input.txt",
                "query": [],
                "headers": {"accept": "text/plain"},
                "payload": None,
            }
        ],
        "final_candidate": None,
        "request_input": None,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "model": "gpt-test",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(output),
                                "annotations": [],
                            }
                        ],
                    },
                    {"id": "reasoning_hidden", "type": "reasoning", "summary": []},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            },
        )

    provider = OpenAIResponsesProvider(
        api_key="secret-test-key",
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    response = provider.generate(
        ContextSnapshot(
            id="context_1",
            run_id="run_1",
            turn_id=1,
            content={"goal": "read", "capabilities": [{"path_template": "/v1/x"}]},
            source_refs=(),
            content_hash="a" * 64,
        ),
        [{"id": "workspace.file.read"}],
        ModelLimits(max_output_tokens=512),
    )

    body = captured["body"]
    assert captured["authorization"] == "Bearer secret-test-key"
    assert body["stream"] is False
    assert body["store"] is False
    assert body["max_output_tokens"] == 512
    assert body["text"]["format"]["type"] == "json_schema"
    assert "tools" not in body
    assert json.loads(body["input"])["context"] == {"goal": "read"}
    assert response.output == output
    assert response.usage["total_tokens"] == 30
    assert response.raw_response["output"][0]["type"] == "message"
    assert all(item["type"] != "reasoning" for item in response.raw_response["output"])
    assert "secret-test-key" not in json.dumps(response.raw_response)


@pytest.mark.parametrize(
    ("provider_type", "model"),
    [
        (OpenAIResponsesProvider, "gpt-test"),
        (OpenAIFunctionToolsProvider, "gpt-test"),
        (DeepSeekResponsesProvider, "deepseek-flash"),
        (DeepSeekFunctionToolsProvider, "deepseek-flash"),
    ],
)
def test_responses_providers_never_dispatch_an_incomplete_response(
    provider_type: type, model: str
) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_incomplete",
                "status": "incomplete",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
            },
        )

    provider = provider_type(
        api_key="test-key",
        model=model,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ModelOutputInvalid):
        provider.generate(
            ContextSnapshot("ctx", "run", 1, {"goal": "do work"}, (), "a" * 64),
            [],
            ModelLimits(max_output_tokens=512),
        )
