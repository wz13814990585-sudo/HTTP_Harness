from __future__ import annotations

from hnh.domain.errors import ExecutionUnavailable
from hnh.ports.execution import CellResult, SandboxHandle


class UnavailableExecutionBroker:
    """Fail-closed broker used when no isolated execution backend is configured."""

    def start(self, profile_id: str) -> SandboxHandle:
        del profile_id
        raise ExecutionUnavailable("No isolated execution broker is configured")

    def execute(self, handle: SandboxHandle, code: str) -> CellResult:
        del handle, code
        raise ExecutionUnavailable("No isolated execution broker is configured")

    def is_alive(self, handle: SandboxHandle) -> bool:
        del handle
        return False

    def stop(self, handle: SandboxHandle) -> None:
        del handle
