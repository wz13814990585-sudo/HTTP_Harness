from __future__ import annotations

import base64
import json
import os
import selectors
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from hnh.domain.errors import EnvironmentLost, ExecutionUnavailable
from hnh.ports.execution import (
    CellResult,
    ExportedFile,
    SandboxHandle,
    SandboxProfile,
)

DAEMON_SCRIPT = r"""
import base64
import contextlib
import io
import json
import pathlib
import sys
import time

PROTOCOL_OUT = sys.__stdout__
NAMESPACE = {"__name__": "__main__"}

class OutputLimitExceeded(Exception):
    pass

class ArtifactLimitExceeded(Exception):
    pass

class LimitedText(io.TextIOBase):
    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.size = 0
    def writable(self):
        return True
    def write(self, value):
        text = str(value)
        encoded = text.encode("utf-8", errors="replace")
        if self.size + len(encoded) > self.limit:
            remaining = max(0, self.limit - self.size)
            if remaining:
                self.parts.append(encoded[:remaining].decode("utf-8", errors="ignore"))
                self.size = self.limit
            raise OutputLimitExceeded("cell output limit exceeded")
        self.parts.append(text)
        self.size += len(encoded)
        return len(text)
    def getvalue(self):
        return "".join(self.parts)

def collect_files(limit):
    root = pathlib.Path("/workspace/outputs")
    if not root.exists():
        return []
    result = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArtifactLimitExceeded("symlink outputs are forbidden")
        if not path.is_file():
            continue
        resolved = path.resolve()
        if root.resolve() not in resolved.parents:
            raise ArtifactLimitExceeded("output path escaped its root")
        content = path.read_bytes()
        total += len(content)
        if total > limit:
            raise ArtifactLimitExceeded("execution artifact limit exceeded")
        result.append({
            "path": path.relative_to(root).as_posix(),
            "content_base64": base64.b64encode(content).decode("ascii"),
        })
    return result

for line in sys.stdin:
    try:
        request = json.loads(line)
        operation = request.get("op")
        if operation == "ping":
            response = {"status": "ready"}
        elif operation == "execute":
            stdout = LimitedText(int(request["output_bytes"]))
            stderr = LimitedText(int(request["output_bytes"]))
            started = time.monotonic()
            status = "succeeded"
            error_type = None
            error_message = None
            try:
                code = compile(str(request["code"]), "<sandbox-cell>", "exec")
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exec(code, NAMESPACE, NAMESPACE)
                files = collect_files(int(request["artifact_bytes"]))
            except OutputLimitExceeded as exc:
                status = "output_limit"
                error_type = type(exc).__name__
                error_message = str(exc)
                files = []
            except ArtifactLimitExceeded as exc:
                status = "artifact_limit"
                error_type = type(exc).__name__
                error_message = str(exc)
                files = []
            except BaseException as exc:
                status = "failed"
                error_type = type(exc).__name__
                error_message = str(exc)[:2048]
                files = []
            response = {
                "status": status,
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
                "error_type": error_type,
                "error_message": error_message,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "files": files,
            }
        else:
            response = {"status": "protocol_error", "error_message": "unknown operation"}
    except BaseException as exc:
        response = {
            "status": "protocol_error",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2048],
        }
    PROTOCOL_OUT.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    PROTOCOL_OUT.flush()
"""


@dataclass(slots=True)
class _DockerSession:
    handle: SandboxHandle
    profile: SandboxProfile
    process: subprocess.Popen[bytes]
    lock: threading.Lock


class DockerExecutionBroker:
    """Trusted Docker broker. No request value becomes an image, mount, or flag."""

    def __init__(self, profiles: tuple[SandboxProfile, ...]) -> None:
        if not profiles:
            raise ValueError("at least one sandbox profile is required")
        self._profiles = {profile.id: profile for profile in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("sandbox profile IDs must be unique")
        self._sessions: dict[str, _DockerSession] = {}
        self._guard = threading.Lock()

    def start(self, profile_id: str) -> SandboxHandle:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ExecutionUnavailable("The requested sandbox profile is not configured")
        sandbox_ref = f"hnh-sandbox-{uuid4().hex}"
        handle = SandboxHandle(sandbox_ref, uuid4().hex, profile.id)
        command = [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--pull=never",
            f"--name={sandbox_ref}",
            "--label=hnh.managed=true",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            f"--cpus={profile.cpu_limit}",
            f"--memory={profile.memory_bytes}",
            f"--memory-swap={profile.memory_bytes}",
            f"--pids-limit={profile.pids_limit}",
            "--ulimit=nofile=64:64",
            "--hostname=sandbox",
            "--workdir=/workspace",
            (f"--tmpfs=/workspace:rw,nosuid,nodev,noexec,size={profile.workspace_bytes},mode=1777"),
            "--env=HOME=/workspace",
            "--env=TMPDIR=/workspace",
            "--log-driver=none",
            "--stop-timeout=1",
            "--entrypoint=python",
            profile.image,
            "-I",
            "-B",
            "-u",
            "-c",
            DAEMON_SCRIPT,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ExecutionUnavailable("Docker could not start the sandbox") from exc
        session = _DockerSession(handle, profile, process, threading.Lock())
        try:
            response = self._request(session, {"op": "ping"}, timeout=15)
            if response.get("status") != "ready":
                raise ExecutionUnavailable("Sandbox did not become ready")
        except Exception:
            self._stop_session(session)
            raise
        with self._guard:
            self._sessions[sandbox_ref] = session
        return handle

    def execute(self, handle: SandboxHandle, code: str) -> CellResult:
        if len(code.encode("utf-8")) > 1024 * 1024:
            raise ValueError("execution code exceeds one MiB")
        session = self._session(handle)
        with session.lock:
            if session.process.poll() is not None:
                raise EnvironmentLost()
            started = time.monotonic()
            try:
                response = self._request(
                    session,
                    {
                        "op": "execute",
                        "code": code,
                        "output_bytes": session.profile.output_bytes,
                        "artifact_bytes": session.profile.artifact_bytes,
                    },
                    timeout=session.profile.timeout_seconds,
                )
            except TimeoutError:
                self._stop_session(session)
                return CellResult(
                    status="timed_out",
                    error_type="TimeoutError",
                    error_message="cell exceeded its execution deadline",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    transport_ok=False,
                )
            except EnvironmentLost as exc:
                self._stop_session(session)
                return CellResult(
                    status="environment_lost",
                    error_type=type(exc).__name__,
                    error_message=exc.detail,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    transport_ok=False,
                )
            files = self._decode_files(response.get("files", []))
            return CellResult(
                status=str(response.get("status", "protocol_error")),
                stdout=str(response.get("stdout", "")),
                stderr=str(response.get("stderr", "")),
                error_type=(
                    None if response.get("error_type") is None else str(response["error_type"])
                ),
                error_message=(
                    None
                    if response.get("error_message") is None
                    else str(response["error_message"])
                ),
                duration_ms=int(response.get("duration_ms", 0)),
                files=files,
                transport_ok=True,
            )

    def is_alive(self, handle: SandboxHandle) -> bool:
        try:
            session = self._session(handle)
        except EnvironmentLost:
            return False
        return session.process.poll() is None

    def stop(self, handle: SandboxHandle) -> None:
        try:
            session = self._session(handle)
        except EnvironmentLost:
            return
        self._stop_session(session)

    def inspect(self, handle: SandboxHandle) -> dict[str, Any]:
        session = self._session(handle)
        try:
            completed = subprocess.run(
                ["docker", "inspect", session.handle.sandbox_ref],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionUnavailable("Docker could not inspect the sandbox") from exc
        payload = json.loads(completed.stdout)
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise ExecutionUnavailable("Docker returned an invalid inspection response")
        return payload[0]

    def close(self) -> None:
        with self._guard:
            sessions = list(self._sessions.values())
        for session in sessions:
            self._stop_session(session)

    def _session(self, handle: SandboxHandle) -> _DockerSession:
        with self._guard:
            session = self._sessions.get(handle.sandbox_ref)
        if session is None or session.handle != handle:
            raise EnvironmentLost()
        return session

    def _request(
        self,
        session: _DockerSession,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        stdin = session.process.stdin
        stdout = session.process.stdout
        if stdin is None or stdout is None:
            raise EnvironmentLost("Sandbox protocol pipes are unavailable")
        try:
            stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise EnvironmentLost() from exc
        maximum = max(
            65536,
            session.profile.output_bytes * 3 + session.profile.artifact_bytes * 2,
        )
        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        collected = bytearray()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError
                chunk = os.read(stdout.fileno(), 4096)
                if not chunk:
                    raise EnvironmentLost()
                collected.extend(chunk)
                if len(collected) > maximum:
                    raise EnvironmentLost("Sandbox exceeded the broker response limit")
                newline = collected.find(b"\n")
                if newline >= 0:
                    raw = bytes(collected[:newline])
                    break
        finally:
            selector.close()
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnvironmentLost("Sandbox returned an invalid protocol response") from exc
        if not isinstance(response, dict):
            raise EnvironmentLost("Sandbox returned a non-object protocol response")
        return response

    def _stop_session(self, session: _DockerSession) -> None:
        with self._guard:
            self._sessions.pop(session.handle.sandbox_ref, None)
        if session.process.poll() is None:
            subprocess.run(
                ["docker", "kill", session.handle.sandbox_ref],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        try:
            session.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            session.process.kill()
            session.process.wait(timeout=5)

    @staticmethod
    def _decode_files(value: Any) -> tuple[ExportedFile, ...]:
        if not isinstance(value, list):
            raise EnvironmentLost("Sandbox files response is invalid")
        files: list[ExportedFile] = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"path", "content_base64"}:
                raise EnvironmentLost("Sandbox file entry is invalid")
            try:
                content = base64.b64decode(str(item["content_base64"]), validate=True)
            except ValueError as exc:
                raise EnvironmentLost("Sandbox returned invalid file bytes") from exc
            files.append(ExportedFile(str(item["path"]), content))
        return tuple(files)
