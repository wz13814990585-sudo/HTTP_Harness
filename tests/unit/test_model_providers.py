from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hnh.adapters.models.openai_responses import (
    OpenAIFunctionToolsProvider,
    OpenAIResponsesProvider,
)
from hnh.domain.errors import ModelOutputInvalid
from hnh.ports.models import ContextSnapshot, ModelLimits


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


@pytest.mark.parametrize("provider_type", [OpenAIResponsesProvider, OpenAIFunctionToolsProvider])
def test_openai_providers_never_dispatch_an_incomplete_response(provider_type: type) -> None:
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
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ModelOutputInvalid):
        provider.generate(
            ContextSnapshot("ctx", "run", 1, {"goal": "do work"}, (), "a" * 64),
            [],
            ModelLimits(max_output_tokens=512),
        )
