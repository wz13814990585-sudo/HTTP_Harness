from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hnh.domain.errors import ProviderUnavailable
from hnh.ports.models import CompleteModelResponse, ContextSnapshot, ModelLimits


class ScriptedProvider:
    """Deterministic test provider. Its results are never reported as live evidence."""

    def __init__(self, responses: Iterable[Any], *, model_revision: str = "scripted-v1") -> None:
        self._responses = iter(responses)
        self._model_revision = model_revision
        self.calls = 0

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model_revision(self) -> str:
        return self._model_revision

    def generate(
        self,
        context: ContextSnapshot,
        tools: list[dict[str, Any]],
        limits: ModelLimits,
    ) -> CompleteModelResponse:
        del context, tools, limits
        self.calls += 1
        try:
            item = next(self._responses)
        except StopIteration as exc:
            raise ProviderUnavailable("scripted response sequence is exhausted") from exc
        if isinstance(item, CompleteModelResponse):
            return item
        return CompleteModelResponse(
            provider_request_id=f"scripted-{self.calls}",
            raw_response={"scripted": True, "sequence": self.calls, "output": item},
            output=item,
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
