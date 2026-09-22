"""Test-only official MCP server for real-socket, separate-process interop."""

from __future__ import annotations

import os
from typing import Any, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

server: MCPServer[Any] = MCPServer("p06-process-fixture", version="1.0.0")


class EchoOutput(TypedDict):
    message: str
    server_pid: int


@server.tool(structured_output=True)
def echo(message: str) -> EchoOutput:
    """Echo a message and the serving OS process ID."""
    return {"message": message, "server_pid": os.getpid()}


app = server.streamable_http_app(
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
