from __future__ import annotations

from collections.abc import Callable

from hnh.application.capabilities import CapabilityRegistry, builtin_capabilities
from hnh.application.mcp import MCPActionCoordinator
from hnh.application.mcp_catalog import MCPIntegrationBinding, MCPToolCatalog
from hnh.application.run_controller import RunController
from hnh.domain.errors import HarnessError, InvalidOperation
from hnh.domain.identity import TrustedContext
from hnh.ports.mcp import MCPClient, MCPProtocolFault

MCPClientFactory = Callable[[MCPIntegrationBinding, TrustedContext], MCPClient]


class MCPIntegrationManager:
    """Trusted request-scoped composition; model input cannot register integrations."""

    def __init__(
        self,
        bindings: tuple[MCPIntegrationBinding, ...],
        client_factory: MCPClientFactory,
        *,
        catalog: MCPToolCatalog | None = None,
    ) -> None:
        identifiers = [binding.integration_id for binding in bindings]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("MCP integration IDs must be unique")
        self._bindings = bindings
        self._client_factory = client_factory
        self._catalog = catalog or MCPToolCatalog()

    def for_context(
        self,
        context: TrustedContext,
        controller: RunController,
    ) -> tuple[CapabilityRegistry, MCPActionCoordinator]:
        capabilities = list(builtin_capabilities())
        clients: dict[str, tuple[MCPIntegrationBinding, MCPClient]] = {}
        for binding in self._bindings:
            if binding.required_scope not in context.scopes:
                continue
            try:
                client = self._client_factory(binding, context)
                imported = self._catalog.import_tools(context, binding, client)
            except MCPProtocolFault as exc:
                raise HarnessError(
                    503,
                    "mcp_discovery_unavailable",
                    "MCP discovery is unavailable",
                    f"integration {binding.integration_id} returned protocol error {exc.code}",
                    "after_refresh",
                ) from exc
            except HarnessError:
                raise
            except Exception as exc:
                raise HarnessError(
                    503,
                    "mcp_discovery_unavailable",
                    "MCP discovery is unavailable",
                    f"integration {binding.integration_id} could not be discovered",
                    "after_refresh",
                ) from exc
            capabilities.extend(imported.capabilities)
            clients[binding.integration_id] = (binding, client)
        ids = [capability.id for capability in capabilities]
        if len(ids) != len(set(ids)):
            raise InvalidOperation("MCP capability IDs conflict with registered capabilities")
        return CapabilityRegistry(tuple(capabilities)), MCPActionCoordinator(controller, clients)
