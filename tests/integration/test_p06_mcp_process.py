"""Independent OS process + real loopback socket, still a local official fixture."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic, sleep
from typing import Any

import httpx2
from mcp.client.streamable_http import streamable_http_client

from hnh.adapters.mcp.sdk import OfficialSDKMCPClient
from hnh.application.mcp_catalog import MCPIntegrationBinding


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_official_mcp_server_in_another_process_is_discovered_over_real_http() -> None:
    port = _available_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.support.mcp_process_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env={name: value for name, value in os.environ.items() if not name.startswith("HNH_")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}/mcp"

    @asynccontextmanager
    async def transport() -> AsyncIterator[Any]:
        async with httpx2.AsyncClient(follow_redirects=False, trust_env=False) as http_client:
            async with streamable_http_client(endpoint, http_client=http_client) as streams:
                yield streams

    binding = MCPIntegrationBinding(
        "p06-process", "modern2026-07-28", tool_effects={"echo": "read_only"}
    )
    try:
        deadline = monotonic() + 10
        while True:
            if process.poll() is not None:
                raise AssertionError("MCP process exited before binding its loopback port")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if monotonic() >= deadline:
                    raise AssertionError("MCP process did not start on loopback") from None
                sleep(0.1)
        discovered = OfficialSDKMCPClient(
            transport(), binding, subject_id="p06-process-subject"
        ).discover()
        assert discovered.protocol_version == "2026-07-28"
        assert {tool.name for tool in discovered.tools} == {"echo"}
        result = OfficialSDKMCPClient(
            transport(), binding, subject_id="p06-process-subject"
        ).call_tool("echo", {"message": "across-process"})
        assert result.structured_content is not None
        assert result.structured_content["message"] == "across-process"
        assert result.structured_content["server_pid"] == process.pid
        assert process.pid != os.getpid()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
