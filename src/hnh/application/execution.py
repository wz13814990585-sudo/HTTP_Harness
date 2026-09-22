from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from hnh.application.resources import ResourceStore
from hnh.application.run_controller import (
    ExecutionSessionSnapshot,
    RunController,
)
from hnh.domain.errors import EnvironmentLost, HarnessError, SchemaInvalid
from hnh.domain.identity import TrustedContext
from hnh.ports.execution import CellResult, ExecutionBroker, SandboxHandle


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    receipt: dict[str, Any]
    succeeded: bool


class ExecutionService:
    """Coordinates trusted broker effects while RunController owns durable state."""

    def __init__(
        self,
        controller: RunController,
        resources: ResourceStore,
        broker: ExecutionBroker,
        *,
        session_ttl_seconds: int = 3600,
    ) -> None:
        self.controller = controller
        self.resources = resources
        self.broker = broker
        self.session_ttl_seconds = session_ttl_seconds
        self._handles: dict[str, SandboxHandle] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def create_session(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        runtime: str,
        profile_id: str,
        source_action_id: str,
    ) -> ExecutionSessionSnapshot:
        if runtime != "python":
            raise SchemaInvalid("only the python runtime is supported")
        self.controller.get_run(context, run_id)
        handle = self.broker.start(profile_id)
        try:
            snapshot = self.controller.admit_execution_session(
                context,
                run_id,
                runtime=runtime,
                profile_id=profile_id,
                generation=handle.generation,
                sandbox_ref=handle.sandbox_ref,
                source_action_id=source_action_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=self.session_ttl_seconds),
            )
        except Exception:
            self.broker.stop(handle)
            raise
        if snapshot.generation != handle.generation:
            self.broker.stop(handle)
            return snapshot
        with self._guard:
            self._handles[snapshot.id] = handle
            self._locks.setdefault(snapshot.id, threading.Lock())
        return snapshot

    def get_session(
        self,
        context: TrustedContext,
        session_id: str,
    ) -> ExecutionSessionSnapshot:
        return self.controller.get_execution_session(context, session_id)

    def reconcile_session(
        self,
        context: TrustedContext,
        session_id: str,
    ) -> ExecutionSessionSnapshot:
        snapshot = self.controller.get_execution_session(context, session_id)
        if snapshot.status not in {"ready", "busy"}:
            return snapshot
        handle = self._handle(session_id)
        if (
            handle is None
            or handle.generation != snapshot.generation
            or not self.broker.is_alive(handle)
        ):
            return self.controller.mark_execution_session_lost(
                context,
                session_id,
                detail="sandbox handle is absent or no longer alive",
            )
        return snapshot

    def execute_cell(
        self,
        context: TrustedContext,
        session_id: str,
        code: str,
        *,
        action_id: str,
        request_hash: str,
    ) -> ExecutionOutcome:
        lock = self._lock(session_id)
        with lock:
            session = self.reconcile_session(context, session_id)
            if session.status != "ready":
                raise EnvironmentLost()
            handle = self._handle(session_id)
            if handle is None or handle.generation != session.generation:
                raise EnvironmentLost()
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            work = self.controller.begin_execution(
                context,
                session_id,
                action_id=action_id,
                generation=session.generation,
                request_hash=request_hash,
                code_hash=code_hash,
            )
            if work.replayed:
                if work.execution.receipt is None:
                    raise EnvironmentLost("A prior execution has no committed receipt")
                return ExecutionOutcome(
                    work.execution.receipt,
                    work.execution.status == "succeeded",
                )
            result = self.broker.execute(handle, code)
            receipt = self._receipt(work.execution.id, session, result)
            if result.succeeded and result.files:
                try:
                    receipt["artifacts"] = self._import_files(
                        context,
                        work.execution.id,
                        action_id,
                        result,
                    )
                except HarnessError as exc:
                    result = CellResult(
                        status="failed",
                        stdout=result.stdout,
                        stderr=result.stderr,
                        error_type="ArtifactImportError",
                        error_message=exc.detail or exc.title,
                        duration_ms=result.duration_ms,
                        transport_ok=result.transport_ok,
                    )
                    receipt = self._receipt(work.execution.id, session, result)
            completed = self.controller.complete_execution(
                work.execution.id,
                status=result.status,
                receipt=receipt,
            )
            if completed.status in {"timed_out", "environment_lost"}:
                with self._guard:
                    self._handles.pop(session_id, None)
            return ExecutionOutcome(receipt, result.succeeded)

    def replace_session(
        self,
        context: TrustedContext,
        session_id: str,
    ) -> ExecutionSessionSnapshot:
        current = self.controller.get_execution_session(context, session_id)
        old_handle = self._handle(session_id)
        if old_handle is not None:
            self.broker.stop(old_handle)
        handle = self.broker.start(current.profile_id)
        try:
            snapshot = self.controller.replace_execution_session(
                context,
                session_id,
                generation=handle.generation,
                sandbox_ref=handle.sandbox_ref,
            )
        except Exception:
            self.broker.stop(handle)
            raise
        with self._guard:
            self._handles[session_id] = handle
        return snapshot

    def stop_session(
        self,
        context: TrustedContext,
        session_id: str,
    ) -> ExecutionSessionSnapshot:
        current = self.controller.get_execution_session(context, session_id)
        handle = self._handle(session_id)
        if handle is not None:
            self.broker.stop(handle)
        with self._guard:
            self._handles.pop(session_id, None)
        if current.status == "lost":
            return current
        return self.controller.stop_execution_session(context, session_id)

    def close(self) -> None:
        with self._guard:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            self.broker.stop(handle)

    def _import_files(
        self,
        context: TrustedContext,
        execution_id: str,
        action_id: str,
        result: CellResult,
    ) -> list[dict[str, Any]]:
        imported: list[dict[str, Any]] = []
        for exported in result.files:
            media_type = mimetypes.guess_type(exported.path)[0] or "application/octet-stream"
            path_hash = hashlib.sha256(exported.path.encode()).hexdigest()[:16]
            artifact = self.resources.create_artifact(
                context,
                exported.content,
                media_type,
                f"execution:{execution_id}:output:{path_hash}",
                source_action_id=action_id,
            )
            imported.append({"path": exported.path, "artifact_id": artifact.id})
        return imported

    @staticmethod
    def _receipt(
        execution_id: str,
        session: ExecutionSessionSnapshot,
        result: CellResult,
    ) -> dict[str, Any]:
        return {
            "execution_id": execution_id,
            "session_id": session.id,
            "generation": session.generation,
            "status": result.status,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": (
                None
                if result.error_type is None
                else {"type": result.error_type, "message": result.error_message or ""}
            ),
            "duration_ms": result.duration_ms,
            "transport_ok": result.transport_ok,
            "artifacts": [],
        }

    def _handle(self, session_id: str) -> SandboxHandle | None:
        with self._guard:
            return self._handles.get(session_id)

    def _lock(self, session_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())


def execution_request_hash(session_id: str, generation: str, code: str) -> str:
    material = json.dumps(
        {
            "session_id": session_id,
            "generation": generation,
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()
