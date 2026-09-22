from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hnh.application.capabilities import Capability
from hnh.domain.errors import InvalidOperation, SchemaInvalid


class QueryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=8192)


class HttpOperation(BaseModel):
    """Untrusted operation proposal. Identity and credentials are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    target: str = Field(min_length=5, max_length=2048)
    query: list[QueryItem] = Field(default_factory=list, max_length=64)
    headers: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, Any] | None = None

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        if not value.startswith("/v1/") or any(char in value for char in "?#\r\n\\"):
            raise ValueError("target must be a relative /v1/ resource path")
        if "://" in value or value.startswith("//"):
            raise ValueError("absolute or network-path targets are forbidden")
        return value

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"accept", "content-type", "if-match", "if-none-match"}
        normalized: dict[str, str] = {}
        for name, item in value.items():
            lower = name.lower()
            if lower not in allowed:
                raise ValueError(f"header {name!r} is not model-controlled")
            if "\r" in item or "\n" in item or len(item) > 8192:
                raise ValueError("header value is invalid")
            normalized[lower] = item
        return normalized

    @field_validator("payload")
    @classmethod
    def validate_payload_envelope(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        kind = value.get("kind")
        expected = {
            "json": {"kind", "value"},
            "text": {"kind", "text"},
            "artifact": {"kind", "artifact_id"},
        }
        if kind not in expected or set(value) != expected[kind]:
            raise ValueError("payload must be one supported, exact envelope")
        if kind == "text" and (not isinstance(value["text"], str) or len(value["text"]) > 1048576):
            raise ValueError("text payload is invalid")
        if kind == "artifact" and not isinstance(value["artifact_id"], str):
            raise ValueError("artifact_id must be a string")
        return value


@dataclass(frozen=True, slots=True)
class BoundOperation:
    capability: Capability
    parameters: dict[str, Any]
    request_hash: str


def operation_fingerprint(operation: HttpOperation, capability: Capability) -> str:
    material = {
        "capability_id": capability.id,
        "capability_revision": capability.revision,
        "operation": operation.model_dump(mode="json", exclude_none=True),
    }
    encoded = json.dumps(
        material, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def require_json_depth(value: Any, maximum: int = 32) -> None:
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > maximum:
            raise SchemaInvalid(f"input exceeds maximum nesting depth {maximum}")
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def payload_value(operation: HttpOperation) -> Any:
    if operation.payload is None:
        return None
    kind = operation.payload["kind"]
    if kind == "json":
        return operation.payload["value"]
    if kind == "text":
        return operation.payload["text"]
    if kind == "artifact":
        return {"artifact_id": operation.payload["artifact_id"]}
    raise InvalidOperation("unsupported payload kind")
