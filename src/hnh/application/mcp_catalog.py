from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from hnh.application.capabilities import Capability
from hnh.application.security import scope_fingerprint
from hnh.domain.errors import InvalidOperation
from hnh.domain.identity import TrustedContext
from hnh.ports.mcp import MCPClient, MCPDiscovery, MCPProfile

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


@dataclass(frozen=True, slots=True)
class MCPIntegrationBinding:
    integration_id: str
    profile: MCPProfile
    required_scope: str = "integrations:invoke"
    policy_revision: str = "dev-1"
    timeout_seconds: int = 30
    tasks_enabled: bool = False
    credential_reference: str | None = None
    issuer: str | None = None
    resource: str | None = None
    tool_effects: dict[str, str] = field(default_factory=dict)
    approval_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ImportedMCPCatalog:
    discovery: MCPDiscovery
    capabilities: tuple[Capability, ...]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    value: ImportedMCPCatalog


class MCPToolCatalog:
    """Import untrusted tool schemas under trusted local bindings and private cache keys."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cache: dict[tuple[str, str, str, str, str, str], _CacheEntry] = {}

    @property
    def cache_keys(self) -> tuple[tuple[str, str, str, str, str, str], ...]:
        return tuple(self._cache)

    def import_tools(
        self,
        context: TrustedContext,
        binding: MCPIntegrationBinding,
        client: MCPClient,
    ) -> ImportedMCPCatalog:
        key = (
            context.tenant_id,
            context.subject_id,
            scope_fingerprint(context.scopes),
            binding.integration_id,
            binding.profile,
            binding.policy_revision,
        )
        cached = self._cache.get(key)
        now = self._clock()
        if cached is not None and cached.expires_at > now:
            return cached.value
        discovery = client.discover()
        if discovery.profile != binding.profile:
            raise InvalidOperation("MCP server negotiated a different configured profile")
        capabilities = tuple(self._capability(binding, discovery, tool) for tool in discovery.tools)
        value = ImportedMCPCatalog(discovery, capabilities)
        if discovery.ttl_ms > 0:
            self._cache[key] = _CacheEntry(now + discovery.ttl_ms / 1000, value)
        return value

    @staticmethod
    def _capability(
        binding: MCPIntegrationBinding,
        discovery: MCPDiscovery,
        tool: Any,
    ) -> Capability:
        if _SAFE_TOOL_NAME.fullmatch(tool.name) is None:
            raise InvalidOperation("MCP tool name cannot be mapped to a local resource path")
        material = {
            "integration_id": binding.integration_id,
            "profile": binding.profile,
            "protocol_version": discovery.protocol_version,
            "name": tool.name,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "policy_revision": binding.policy_revision,
        }
        revision = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        modes = ["complete"]
        if binding.profile == "modern2026-07-28":
            modes.append("input_required")
        if binding.tasks_enabled and TASKS_EXTENSION_ID in discovery.extensions:
            modes.append("task")
        return Capability(
            id=f"mcp.{binding.integration_id}.{tool.name}",
            revision=revision,
            description=tool.description or f"MCP tool {tool.name}",
            method="POST",
            path_template=(
                f"/v1/integrations/{binding.integration_id}/operations/{tool.name}/invocations"
            ),
            request_media_types=("application/json",),
            input_schema=tool.input_schema,
            output_schema=tool.output_schema or {},
            effect_semantics=binding.tool_effects.get(tool.name, "unsafe"),
            timeout_seconds=binding.timeout_seconds,
            requires_approval=tool.name in binding.approval_tools,
            supported_result_modes=tuple(modes),
            required_scope=binding.required_scope,
        )
