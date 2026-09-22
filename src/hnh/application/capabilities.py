from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from hnh.domain.errors import InvalidOperation, PermissionDenied, ResourceNotFound
from hnh.domain.identity import TrustedContext


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    revision: str
    description: str
    method: str
    path_template: str
    request_media_types: tuple[str, ...]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effect_semantics: str
    timeout_seconds: int
    requires_approval: bool
    supported_result_modes: tuple[str, ...]
    required_scope: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision": self.revision,
            "description": self.description,
            "method": self.method,
            "path_template": self.path_template,
            "request_media_types": list(self.request_media_types),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "effect_semantics": self.effect_semantics,
            "timeout_seconds": self.timeout_seconds,
            "requires_approval": self.requires_approval,
            "supported_result_modes": list(self.supported_result_modes),
        }


@dataclass(frozen=True, slots=True)
class CapabilityPage:
    items: tuple[Capability, ...]
    catalog_revision: str
    next_cursor: str | None


EMPTY_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": False}
ARTIFACT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["id", "media_type", "size_bytes", "sha256", "content_url", "created_at"],
}
FILE_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["workspace_id", "path", "revision", "etag", "artifact_id"],
}


def builtin_capabilities() -> tuple[Capability, ...]:
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    file_path = {"type": "string", "minLength": 1, "maxLength": 1024}
    return (
        Capability(
            "artifact.create",
            "1",
            "Store immutable artifact bytes.",
            "POST",
            "/v1/artifacts",
            ("application/octet-stream",),
            {
                "type": "object",
                "properties": {
                    "content_base64": {"type": "string", "maxLength": 1398104},
                    "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
                },
                "required": ["content_base64", "media_type"],
                "additionalProperties": False,
            },
            ARTIFACT_OUTPUT,
            "local_transactional",
            10,
            False,
            ("complete",),
            "artifacts:write",
        ),
        Capability(
            "artifact.content.read",
            "1",
            "Read immutable artifact bytes.",
            "GET",
            "/v1/artifacts/{artifact_id}/content",
            ("application/octet-stream",),
            {
                "type": "object",
                "properties": {"artifact_id": identifier},
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            {},
            "read_only",
            10,
            False,
            ("complete",),
            "artifacts:read",
        ),
        Capability(
            "artifact.read",
            "1",
            "Read immutable artifact metadata.",
            "GET",
            "/v1/artifacts/{artifact_id}",
            ("application/json",),
            {
                "type": "object",
                "properties": {"artifact_id": identifier},
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            ARTIFACT_OUTPUT,
            "read_only",
            10,
            False,
            ("complete",),
            "artifacts:read",
        ),
        Capability(
            "execution.python",
            "1",
            "Execute one text cell in an existing isolated Python generation.",
            "POST",
            "/v1/execution-sessions/{session_id}/executions",
            ("text/plain",),
            {
                "type": "object",
                "properties": {
                    "session_id": identifier,
                    "code": {"type": "string", "maxLength": 1048576},
                },
                "required": ["session_id", "code"],
                "additionalProperties": False,
            },
            {"type": "object"},
            "unsafe",
            120,
            False,
            ("complete",),
            "execution:write",
        ),
        Capability(
            "execution.session.create",
            "1",
            "Create a Python session from a trusted server-side sandbox profile.",
            "POST",
            "/v1/execution-sessions",
            ("application/json",),
            {
                "type": "object",
                "properties": {
                    "run_id": identifier,
                    "runtime": {"const": "python"},
                    "profile_id": identifier,
                },
                "required": ["run_id", "runtime", "profile_id"],
                "additionalProperties": False,
            },
            {"type": "object"},
            "local_transactional",
            30,
            False,
            ("complete",),
            "execution:write",
        ),
        Capability(
            "execution.session.read",
            "1",
            "Read an authorized execution session and its generation.",
            "GET",
            "/v1/execution-sessions/{session_id}",
            ("application/json",),
            {
                "type": "object",
                "properties": {"session_id": identifier},
                "required": ["session_id"],
                "additionalProperties": False,
            },
            {"type": "object"},
            "read_only",
            5,
            False,
            ("complete",),
            "execution:read",
        ),
        Capability(
            "native.echo",
            "1",
            "Deterministic local echo used to verify adapter-independent dispatch.",
            "POST",
            "/v1/integrations/native/operations/echo/invocations",
            ("application/json",),
            {
                "type": "object",
                "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 100}},
                "required": ["message"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            "read_only",
            5,
            False,
            ("complete",),
            "integrations:invoke",
        ),
        Capability(
            "workspace.file.read",
            "1",
            "Read a versioned managed workspace file.",
            "GET",
            "/v1/workspaces/{workspace_id}/files/{file_path}",
            ("application/octet-stream", "text/plain"),
            {
                "type": "object",
                "properties": {"workspace_id": identifier, "file_path": file_path},
                "required": ["workspace_id", "file_path"],
                "additionalProperties": False,
            },
            {},
            "read_only",
            10,
            False,
            ("complete",),
            "workspace:read",
        ),
        Capability(
            "workspace.file.write",
            "1",
            "Conditionally write a versioned managed workspace file.",
            "PUT",
            "/v1/workspaces/{workspace_id}/files/{file_path}",
            ("text/plain",),
            {
                "type": "object",
                "properties": {
                    "workspace_id": identifier,
                    "file_path": file_path,
                    "text": {"type": "string", "maxLength": 1048576},
                    "if_match": {"type": "string", "minLength": 1},
                    "if_none_match": {"const": "*"},
                },
                "required": ["workspace_id", "file_path", "text"],
                "oneOf": [
                    {"required": ["if_match"], "not": {"required": ["if_none_match"]}},
                    {"required": ["if_none_match"], "not": {"required": ["if_match"]}},
                ],
                "additionalProperties": False,
            },
            FILE_OUTPUT,
            "local_transactional",
            10,
            False,
            ("complete",),
            "workspace:write",
        ),
    )


class CapabilityRegistry:
    """Versioned trusted catalog. Cached discovery never grants dispatch authority."""

    def __init__(self, capabilities: tuple[Capability, ...] | None = None) -> None:
        ordered = sorted(capabilities or builtin_capabilities(), key=lambda item: item.id)
        self._items = tuple(ordered)
        material = json.dumps(
            [item.public() for item in ordered], sort_keys=True, separators=(",", ":")
        )
        self.catalog_revision = hashlib.sha256(material.encode()).hexdigest()[:16]
        self._cache: dict[tuple[str, str, str, str, int], CapabilityPage] = {}

    @property
    def cache_keys(self) -> tuple[tuple[str, str, str, str, int], ...]:
        return tuple(self._cache)

    def list(self, context: TrustedContext, cursor: str | None, limit: int) -> CapabilityPage:
        if not 1 <= limit <= 100:
            raise InvalidOperation("limit must be between 1 and 100")
        scope_fingerprint = self._scope_fingerprint(context)
        cache_key = (context.tenant_id, context.subject_id, scope_fingerprint, cursor or "", limit)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        visible = tuple(item for item in self._items if item.required_scope in context.scopes)
        start = self._decode_cursor(cursor, visible)
        page_items = visible[start : start + limit]
        next_cursor = None
        if start + limit < len(visible):
            next_cursor = self._encode_cursor(page_items[-1].id)
        page = CapabilityPage(page_items, self.catalog_revision, next_cursor)
        self._cache[cache_key] = page
        return page

    def get(self, context: TrustedContext, capability_id: str) -> Capability:
        capability = next((item for item in self._items if item.id == capability_id), None)
        if capability is None or capability.required_scope not in context.scopes:
            raise ResourceNotFound("capability")
        return capability

    def resolve(
        self, context: TrustedContext, method: str, target: str
    ) -> tuple[Capability, dict[str, str]]:
        for capability in self._items:
            if capability.method != method:
                continue
            match = self._match(capability.path_template, target)
            if match is None:
                continue
            if capability.required_scope not in context.scopes:
                raise PermissionDenied()
            return capability, match
        raise ResourceNotFound("capability")

    def openapi(self, context: TrustedContext) -> dict[str, Any]:
        visible = [item for item in self._items if item.required_scope in context.scopes]
        paths: dict[str, Any] = {}
        for item in visible:
            paths.setdefault(item.path_template, {})[item.method.lower()] = {
                "operationId": item.id.replace(".", "_").replace("-", "_"),
                "description": item.description,
                "x-hnh-capability-revision": item.revision,
                "responses": {"200": {"description": "Capability result"}},
            }
        return {
            "openapi": "3.1.1",
            "info": {
                "title": "HTTP-native Harness implemented capabilities",
                "version": self.catalog_revision,
            },
            "paths": paths,
        }

    def _encode_cursor(self, last_id: str) -> str:
        raw = json.dumps(
            {"revision": self.catalog_revision, "last": last_id}, separators=(",", ":")
        )
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    def _decode_cursor(self, cursor: str | None, visible: tuple[Capability, ...]) -> int:
        if cursor is None:
            return 0
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            if (
                not isinstance(value, dict)
                or set(value) != {"revision", "last"}
                or value["revision"] != self.catalog_revision
                or not isinstance(value["last"], str)
            ):
                raise ValueError
            ids = [item.id for item in visible]
            return ids.index(str(value["last"])) + 1
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidOperation("cursor is invalid or belongs to another catalog") from exc

    @staticmethod
    def _scope_fingerprint(context: TrustedContext) -> str:
        return hashlib.sha256("\0".join(sorted(context.scopes)).encode()).hexdigest()[:16]

    @staticmethod
    def _match(template: str, target: str) -> dict[str, str] | None:
        parts = re.split(r"(\{[^}]+\})", template)
        pattern = ""
        names: list[str] = []
        for part in parts:
            if part.startswith("{"):
                name = part[1:-1]
                names.append(name)
                pattern += f"(?P<{name}>.+)" if name == "file_path" else f"(?P<{name}>[^/]+)"
            else:
                pattern += re.escape(part)
        match = re.fullmatch(pattern, target)
        return None if match is None else match.groupdict()
