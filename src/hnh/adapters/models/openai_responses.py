from __future__ import annotations

import json
from typing import Any

import httpx

from hnh.adapters.models.function_tools import FunctionToolCatalog
from hnh.domain.errors import ModelOutputInvalid, ProviderUnavailable
from hnh.ports.models import CompleteModelResponse, ContextSnapshot, ModelLimits

HTTP_OPERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
        "target": {"type": "string", "pattern": r"^/v1/[^?#\r\n\\]*$"},
        "query": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                "required": ["name", "value"],
                "additionalProperties": False,
            },
        },
        "headers": {
            "type": "object",
            "properties": {
                "accept": {"type": "string"},
                "content-type": {"type": "string"},
                "if-match": {"type": "string"},
                "if-none-match": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "payload": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {"kind": {"const": "json"}, "value": {}},
                    "required": ["kind", "value"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"kind": {"const": "text"}, "text": {"type": "string"}},
                    "required": ["kind", "text"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "artifact"},
                        "artifact_id": {"type": "string"},
                    },
                    "required": ["kind", "artifact_id"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": ["method", "target", "query", "headers", "payload"],
    "additionalProperties": False,
}

FINAL_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "artifact_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "acceptance_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "artifact_ids", "evidence_ids", "acceptance_claims"],
    "additionalProperties": False,
}

MODEL_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "public_output": {"type": "string"},
        "operations": {"type": "array", "items": HTTP_OPERATION_SCHEMA},
        "final_candidate": {"anyOf": [{"type": "null"}, FINAL_CANDIDATE_SCHEMA]},
        "request_input": {"type": ["string", "null"]},
    },
    "required": ["public_output", "operations", "final_candidate", "request_input"],
    "additionalProperties": False,
}

FUNCTION_TEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "public_output": {"type": "string"},
        "final_candidate": {"anyOf": [{"type": "null"}, FINAL_CANDIDATE_SCHEMA]},
        "request_input": {"type": ["string", "null"]},
    },
    "required": ["public_output", "final_candidate", "request_input"],
    "additionalProperties": False,
}


class OpenAIResponsesProvider:
    """Configured non-streaming OpenAI Responses API adapter without hosted tools."""

    _provider_display_name = "OpenAI"
    _provider_name = "openai-responses"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def model_revision(self) -> str:
        return self._model

    def _provider_request_options(self) -> dict[str, Any]:
        """Trusted provider-specific request fields.

        These values come from administrator configuration, never from model
        output or a capability description.
        """

        return {}

    def generate(
        self,
        context: ContextSnapshot,
        tools: list[dict[str, Any]],
        limits: ModelLimits,
    ) -> CompleteModelResponse:
        # Keep the task context identical across the two model-facing views.
        # The operation catalog is supplied exactly once in each surface.
        task_context = {
            key: value for key, value in context.content.items() if key != "capabilities"
        }
        body = {
            "model": self._model,
            "instructions": (
                "Return one decision matching the supplied JSON schema. Propose only relative "
                "/v1 operations from the authorized capability catalog. Never claim completion "
                "without artifact and evidence identifiers from committed observations."
            ),
            "input": json.dumps(
                {
                    "context": task_context,
                    "source_refs": context.source_refs,
                    "capabilities": tools,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "hnh_model_turn",
                    "strict": True,
                    "schema": MODEL_TURN_SCHEMA,
                }
            },
            "max_output_tokens": limits.max_output_tokens,
            "store": False,
            "stream": False,
        }
        body.update(self._provider_request_options())
        try:
            response = self._client.post(
                f"{self._base_url}/responses",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(
                f"{self._provider_display_name} Responses API request failed"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
            raise ProviderUnavailable(
                f"{self._provider_display_name} response was not a response object"
            )
        if payload.get("status") != "completed":
            raise ModelOutputInvalid(f"{self._provider_display_name} response did not complete")
        text = self._output_text(payload)
        try:
            output = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelOutputInvalid(
                f"{self._provider_display_name} structured output was not valid JSON"
            ) from exc
        usage = payload.get("usage")
        return CompleteModelResponse(
            provider_request_id=str(payload["id"]) if payload.get("id") is not None else None,
            raw_response=self._public_response(payload),
            output=output,
            usage=dict(usage) if isinstance(usage, dict) else {},
        )

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        chunks: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    chunks.append(str(content.get("text", "")))
        if not chunks:
            raise ModelOutputInvalid("OpenAI response contained no output_text")
        return "".join(chunks)

    @staticmethod
    def _public_response(payload: dict[str, Any]) -> dict[str, Any]:
        public_output = [
            item
            for item in payload.get("output", [])
            if isinstance(item, dict) and item.get("type") == "message"
        ]
        return {
            key: payload[key]
            for key in ("id", "model", "status", "error", "incomplete_details", "usage")
            if key in payload
        } | {"output": public_output}


class OpenAIFunctionToolsProvider(OpenAIResponsesProvider):
    """Responses function-call surface over the same authorized catalog.

    Calls are decoded into proposals. Runner persists the full completed
    response before ActionGateway binds or dispatches anything.
    """

    _function_provider_name = "openai-function-tools"

    @property
    def name(self) -> str:
        return self._function_provider_name

    def generate(
        self,
        context: ContextSnapshot,
        tools: list[dict[str, Any]],
        limits: ModelLimits,
    ) -> CompleteModelResponse:
        catalog = FunctionToolCatalog(tools)
        # The shared ContextSnapshot contains a compact HTTP catalog for the
        # HTTP-semantic arm. Do not leak that second operation interface into
        # the function-schema arm of the comparison.
        native_context = {
            key: value for key, value in context.content.items() if key != "capabilities"
        }
        body = {
            "model": self._model,
            "instructions": (
                "Use the supplied functions for capability operations. Never treat a function "
                "call as already executed. If no operation is needed, return a JSON decision "
                "with exactly one final_candidate or request_input. "
                "Do not claim completion without committed artifact and evidence identifiers."
            ),
            "input": json.dumps(
                {"context": native_context, "source_refs": context.source_refs},
                ensure_ascii=False,
                sort_keys=True,
            ),
            "tools": catalog.tools,
            "tool_choice": "auto",
            # Match the HTTP-semantic arm's ability to propose several
            # independent operations in one complete model turn. Runner
            # still applies policy and budget to each admitted action.
            "parallel_tool_calls": True,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "hnh_function_text_decision",
                    "strict": True,
                    "schema": FUNCTION_TEXT_SCHEMA,
                }
            },
            "max_output_tokens": limits.max_output_tokens,
            "store": False,
            "stream": False,
        }
        body.update(self._provider_request_options())
        try:
            response = self._client.post(
                f"{self._base_url}/responses",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(
                f"{self._provider_display_name} Responses API request failed"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
            raise ProviderUnavailable(
                f"{self._provider_display_name} response was not a completed response object"
            )
        if payload.get("status") != "completed":
            raise ModelOutputInvalid(f"{self._provider_display_name} response did not complete")
        calls = [
            item
            for item in payload["output"]
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if calls:
            if len(calls) > 16 or any(
                isinstance(item, dict) and item.get("type") == "message"
                for item in payload["output"]
            ):
                raise ModelOutputInvalid("function response mixed incompatible decisions")
            operations = []
            call_ids: set[str] = set()
            for call in calls:
                name = call.get("name")
                arguments = call.get("arguments")
                call_id = call.get("call_id")
                if (
                    not isinstance(name, str)
                    or not isinstance(arguments, str)
                    or not isinstance(call_id, str)
                    or not call_id
                    or call_id in call_ids
                ):
                    raise ModelOutputInvalid("function call has missing or duplicate fields")
                call_ids.add(call_id)
                operations.append(catalog.decode(name, arguments).model_dump(mode="json"))
            output: dict[str, Any] = {
                "public_output": "",
                "operations": operations,
                "final_candidate": None,
                "request_input": None,
            }
        else:
            try:
                decision = json.loads(self._output_text(payload))
            except json.JSONDecodeError as exc:
                raise ModelOutputInvalid(
                    f"{self._provider_display_name} structured output was not valid JSON"
                ) from exc
            if not isinstance(decision, dict) or set(decision) != {
                "public_output",
                "final_candidate",
                "request_input",
            }:
                raise ModelOutputInvalid("function provider text is not a final/input decision")
            output = {**decision, "operations": []}
        usage = payload.get("usage")
        raw = self._public_response(payload)
        raw["output"] = [
            item
            for item in payload["output"]
            if isinstance(item, dict) and item.get("type") in {"message", "function_call"}
        ]
        return CompleteModelResponse(
            provider_request_id=str(payload["id"]) if payload.get("id") is not None else None,
            raw_response=raw,
            output=output,
            usage=dict(usage) if isinstance(usage, dict) else {},
        )
