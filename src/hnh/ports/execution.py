from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    """Trusted server-side limits. Callers select only the profile ID."""

    id: str
    image: str
    cpu_limit: float = 0.5
    memory_bytes: int = 64 * 1024 * 1024
    pids_limit: int = 32
    timeout_seconds: float = 2.0
    output_bytes: int = 16 * 1024
    artifact_bytes: int = 1024 * 1024
    workspace_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.id or not self.image:
            raise ValueError("sandbox profile ID and image are required")
        if "@sha256:" not in self.image:
            raise ValueError("sandbox images must be pinned by sha256 digest")
        if self.cpu_limit <= 0 or self.memory_bytes < 16 * 1024 * 1024:
            raise ValueError("sandbox CPU and memory limits are invalid")
        if self.pids_limit < 4 or self.timeout_seconds <= 0:
            raise ValueError("sandbox PID and timeout limits are invalid")
        if min(self.output_bytes, self.artifact_bytes, self.workspace_bytes) <= 0:
            raise ValueError("sandbox byte limits must be positive")


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    sandbox_ref: str
    generation: str
    profile_id: str


@dataclass(frozen=True, slots=True)
class ExportedFile:
    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class CellResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    files: tuple[ExportedFile, ...] = field(default_factory=tuple)
    transport_ok: bool = True

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


class ExecutionBroker(Protocol):
    def start(self, profile_id: str) -> SandboxHandle: ...

    def execute(self, handle: SandboxHandle, code: str) -> CellResult: ...

    def is_alive(self, handle: SandboxHandle) -> bool: ...

    def stop(self, handle: SandboxHandle) -> None: ...


@dataclass(frozen=True, slots=True)
class BlobReceipt:
    storage_key: str
    sha256: str
    size_bytes: int


class BlobStore(Protocol):
    def put(self, content: bytes) -> BlobReceipt: ...

    def get(self, storage_key: str) -> bytes: ...

    def list_keys(self) -> set[str]: ...

    def delete(self, storage_key: str) -> None: ...
