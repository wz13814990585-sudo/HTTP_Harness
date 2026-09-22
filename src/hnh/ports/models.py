from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hnh.application.operations import HttpOperation


class FinalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    acceptance_claims: list[str] = Field(default_factory=list)


class ModelTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_output: str = ""
    operations: list[HttpOperation] = Field(default_factory=list)
    final_candidate: FinalCandidate | None = None
    request_input: str | None = None

    @model_validator(mode="after")
    def exactly_one_decision(self) -> ModelTurn:
        decisions = (
            int(bool(self.operations))
            + int(self.final_candidate is not None)
            + int(self.request_input is not None)
        )
        if decisions != 1:
            raise ValueError("a model turn must contain exactly one decision kind")
        return self


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    id: str
    run_id: str
    turn_id: int
    content: dict[str, Any]
    source_refs: tuple[dict[str, Any], ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class ModelLimits:
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class CompleteModelResponse:
    provider_request_id: str | None
    raw_response: dict[str, Any]
    output: Any
    usage: dict[str, Any]


class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model_revision(self) -> str: ...

    def generate(
        self,
        context: ContextSnapshot,
        tools: list[dict[str, Any]],
        limits: ModelLimits,
    ) -> CompleteModelResponse: ...
