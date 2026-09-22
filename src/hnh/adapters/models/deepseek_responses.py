from __future__ import annotations

from typing import Any

import httpx

from hnh.adapters.models.openai_responses import (
    OpenAIFunctionToolsProvider,
    OpenAIResponsesProvider,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
DEEPSEEK_REASONING_EFFORTS = frozenset({"none", "low", "high", "max"})


def validate_deepseek_configuration(model: str, reasoning_effort: str) -> None:
    if model not in DEEPSEEK_MODELS:
        supported = ", ".join(sorted(DEEPSEEK_MODELS))
        raise ValueError(f"unsupported DeepSeek model; expected one of: {supported}")
    if reasoning_effort not in DEEPSEEK_REASONING_EFFORTS:
        supported = ", ".join(sorted(DEEPSEEK_REASONING_EFFORTS))
        raise ValueError(f"unsupported DeepSeek reasoning effort; expected one of: {supported}")


class DeepSeekResponsesProvider(OpenAIResponsesProvider):
    """DeepSeek's stateless Responses API using HTTP-semantic operations.

    The adapter deliberately persists only public message output. DeepSeek
    reasoning items are provider-internal deliberation and are not copied into
    the Harness context or audit response.
    """

    _provider_display_name = "DeepSeek"
    _provider_name = "deepseek-responses"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = DEEPSEEK_BASE_URL,
        reasoning_effort: str = "none",
        timeout_seconds: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        validate_deepseek_configuration(model, reasoning_effort)
        self._reasoning_effort = reasoning_effort
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            client=client,
        )

    def _provider_request_options(self) -> dict[str, Any]:
        return {"reasoning": {"effort": self._reasoning_effort}, "temperature": 0.0}

    def _text_format(self, name: str, schema: dict[str, Any]) -> dict[str, Any]:
        del name, schema
        # The Harness HttpOperation payload intentionally accepts arbitrary JSON
        # before capability-specific validation. DeepSeek's strict schema dialect
        # rejects that open value, so use JSON mode and retain deterministic local
        # validation before Action admission.
        return {"type": "json_object"}

    def _input_contract(self, schema: dict[str, Any]) -> dict[str, Any]:
        return {"output_contract": schema}


class DeepSeekFunctionToolsProvider(OpenAIFunctionToolsProvider):
    """DeepSeek Responses function-tool view over the authorized catalog."""

    _provider_display_name = "DeepSeek"
    _provider_name = "deepseek-responses"
    _function_provider_name = "deepseek-function-tools"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = DEEPSEEK_BASE_URL,
        reasoning_effort: str = "none",
        timeout_seconds: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        validate_deepseek_configuration(model, reasoning_effort)
        self._reasoning_effort = reasoning_effort
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            client=client,
        )

    def _provider_request_options(self) -> dict[str, Any]:
        return {"reasoning": {"effort": self._reasoning_effort}, "temperature": 0.0}

    def _text_format(self, name: str, schema: dict[str, Any]) -> dict[str, Any]:
        del name, schema
        return {"type": "json_object"}

    def _input_contract(self, schema: dict[str, Any]) -> dict[str, Any]:
        return {"output_contract": schema}
