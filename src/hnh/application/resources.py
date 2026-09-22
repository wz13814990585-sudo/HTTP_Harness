from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from hnh.adapters.postgres.database import build_session_factory
from hnh.adapters.postgres.models import ArtifactRecord, IdempotencyRecord, WorkspaceEntryRecord
from hnh.application.run_controller import request_fingerprint
from hnh.domain.errors import (
    DependencyUnavailable,
    IdempotencyConflict,
    InvalidOperation,
    PayloadTooLarge,
    PreconditionFailed,
    PreconditionRequired,
    ResourceNotFound,
)
from hnh.domain.identity import TrustedContext
from hnh.ports.execution import BlobStore


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _lock_key(*parts: str) -> int:
    digest = hashlib.sha256("\0".join(parts).encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def normalize_file_path(value: str) -> str:
    if not value or len(value) > 1024:
        raise InvalidOperation("file path length is invalid")
    if value.startswith("/") or "\\" in value or "\0" in value or "%" in value:
        raise InvalidOperation("file path is not a canonical relative path")
    normalized = unicodedata.normalize("NFC", value)
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidOperation("file path contains a forbidden segment")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    id: str
    media_type: str
    size_bytes: int
    sha256: str
    source_action_id: str | None
    created_at: datetime

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_url": f"/v1/artifacts/{self.id}/content",
            "created_at": self.created_at.isoformat(),
        }
        if self.source_action_id is not None:
            result["source_action_id"] = self.source_action_id
        return result


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    workspace_id: str
    path: str
    revision: int
    etag: str
    artifact_id: str

    def public(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "revision": self.revision,
            "etag": self.etag,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True, slots=True)
class FileContent:
    snapshot: FileSnapshot
    content: bytes
    media_type: str


class ResourceStore:
    def __init__(self, engine: Engine, blob_store: BlobStore | None = None) -> None:
        self._sessions: sessionmaker[Session] = build_session_factory(engine)
        self._blob_store = blob_store

    def create_artifact(
        self,
        context: TrustedContext,
        content: bytes,
        media_type: str,
        idempotency_key: str,
        *,
        source_action_id: str | None = None,
        after_blob_before_metadata: Callable[[], None] | None = None,
    ) -> ArtifactSnapshot:
        if len(content) > 1048576:
            raise PayloadTooLarge()
        route = "/v1/artifacts"
        payload = {"media_type": media_type, "sha256": hashlib.sha256(content).hexdigest()}
        fingerprint = request_fingerprint("POST", route, payload)
        with self._sessions.begin() as session:
            self._advisory_lock(session, context, "POST", route, idempotency_key)
            replay = self._idempotency(
                session, context, "POST", route, idempotency_key, fingerprint
            )
            if replay is not None:
                artifact = session.get(ArtifactRecord, replay.resource_id)
                if artifact is None:
                    raise RuntimeError("artifact idempotency record is dangling")
                return self._artifact_snapshot(artifact)
            artifact = self._new_artifact(
                context,
                content,
                media_type,
                source_action_id,
                after_blob_before_metadata=after_blob_before_metadata,
            )
            session.add(artifact)
            session.flush()
            snapshot = self._artifact_snapshot(artifact)
            self._record_idempotency(
                session,
                context,
                "POST",
                route,
                idempotency_key,
                fingerprint,
                snapshot.id,
                f"/v1/artifacts/{snapshot.id}",
                snapshot.public(),
                201,
            )
            return snapshot

    def get_artifact(self, context: TrustedContext, artifact_id: str) -> ArtifactSnapshot:
        with self._sessions() as session:
            artifact = self._visible_artifact(session, context, artifact_id)
            return self._artifact_snapshot(artifact)

    def get_artifact_content(
        self, context: TrustedContext, artifact_id: str
    ) -> tuple[bytes, str, str]:
        with self._sessions() as session:
            artifact = self._visible_artifact(session, context, artifact_id)
            return (
                self._artifact_bytes(artifact),
                artifact.media_type,
                f'"sha256:{artifact.sha256}"',
            )

    def read_file(self, context: TrustedContext, workspace_id: str, path: str) -> FileContent:
        canonical = normalize_file_path(path)
        with self._sessions() as session:
            row = session.scalar(
                select(WorkspaceEntryRecord).where(
                    WorkspaceEntryRecord.tenant_id == context.tenant_id,
                    WorkspaceEntryRecord.subject_id == context.subject_id,
                    WorkspaceEntryRecord.workspace_id == workspace_id,
                    WorkspaceEntryRecord.logical_path == canonical,
                )
            )
            if row is None:
                raise ResourceNotFound("workspace file")
            artifact = self._visible_artifact(session, context, row.artifact_id)
            return FileContent(
                self._file_snapshot(row), self._artifact_bytes(artifact), artifact.media_type
            )

    def write_file(
        self,
        context: TrustedContext,
        workspace_id: str,
        path: str,
        content: bytes,
        idempotency_key: str,
        *,
        if_match: str | None,
        if_none_match: str | None,
        source_action_id: str | None = None,
    ) -> tuple[FileSnapshot, bool]:
        canonical = normalize_file_path(path)
        if len(content) > 1048576:
            raise PayloadTooLarge()
        if (if_match is None) == (if_none_match is None):
            raise PreconditionRequired()
        if if_none_match is not None and if_none_match != "*":
            raise PreconditionFailed()
        route = f"/v1/workspaces/{workspace_id}/files/{canonical}"
        payload = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "if_match": if_match,
            "if_none_match": if_none_match,
        }
        fingerprint = request_fingerprint("PUT", route, payload)
        with self._sessions.begin() as session:
            self._advisory_lock(session, context, "PUT", route, idempotency_key)
            replay = self._idempotency(session, context, "PUT", route, idempotency_key, fingerprint)
            if replay is not None:
                return self._snapshot_from_body(replay.response_body), replay.response_status == 201
            session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        _lock_key(context.tenant_id, context.subject_id, workspace_id, canonical)
                    )
                )
            )
            row = session.scalar(
                select(WorkspaceEntryRecord)
                .where(
                    WorkspaceEntryRecord.tenant_id == context.tenant_id,
                    WorkspaceEntryRecord.subject_id == context.subject_id,
                    WorkspaceEntryRecord.workspace_id == workspace_id,
                    WorkspaceEntryRecord.logical_path == canonical,
                )
                .with_for_update()
            )
            created = row is None
            if row is None:
                if if_none_match != "*":
                    raise PreconditionFailed()
                revision = 1
            else:
                if if_match is None or if_match != row.etag:
                    raise PreconditionFailed()
                revision = row.revision + 1
            artifact = self._new_artifact(context, content, "text/plain", source_action_id)
            session.add(artifact)
            session.flush()
            etag = f'"sha256:{artifact.sha256}:r{revision}"'
            if row is None:
                row = WorkspaceEntryRecord(
                    id=_identifier("file"),
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    workspace_id=workspace_id,
                    logical_path=canonical,
                    revision=revision,
                    etag=etag,
                    artifact_id=artifact.id,
                )
                session.add(row)
            else:
                row.revision = revision
                row.etag = etag
                row.artifact_id = artifact.id
            session.flush()
            snapshot = self._file_snapshot(row)
            status = 201 if created else 200
            self._record_idempotency(
                session,
                context,
                "PUT",
                route,
                idempotency_key,
                fingerprint,
                artifact.id,
                route,
                snapshot.public(),
                status,
            )
            return snapshot, created

    def _new_artifact(
        self,
        context: TrustedContext,
        content: bytes,
        media_type: str,
        source_action_id: str | None,
        *,
        after_blob_before_metadata: Callable[[], None] | None = None,
    ) -> ArtifactRecord:
        storage_key: str | None = None
        database_content: bytes | None = content
        if self._blob_store is not None:
            receipt = self._blob_store.put(content)
            storage_key = receipt.storage_key
            database_content = None
            if after_blob_before_metadata is not None:
                after_blob_before_metadata()
        return ArtifactRecord(
            id=_identifier("artifact"),
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            media_type=media_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=database_content,
            storage_key=storage_key,
            source_action_id=source_action_id,
        )

    def garbage_collect_blobs(self) -> tuple[str, ...]:
        if self._blob_store is None:
            return ()
        with self._sessions() as session:
            registered = {
                key
                for key in session.scalars(
                    select(ArtifactRecord.storage_key).where(
                        ArtifactRecord.storage_key.is_not(None)
                    )
                )
                if key is not None
            }
        orphaned = sorted(self._blob_store.list_keys() - registered)
        for storage_key in orphaned:
            self._blob_store.delete(storage_key)
        return tuple(orphaned)

    def _artifact_bytes(self, artifact: ArtifactRecord) -> bytes:
        if artifact.content is not None:
            content = bytes(artifact.content)
        elif artifact.storage_key is not None and self._blob_store is not None:
            try:
                content = self._blob_store.get(artifact.storage_key)
            except FileNotFoundError as exc:
                raise DependencyUnavailable("Artifact blob") from exc
        else:
            raise DependencyUnavailable("Artifact blob store")
        if (
            len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise DependencyUnavailable("Artifact blob integrity")
        return content

    @staticmethod
    def _visible_artifact(
        session: Session, context: TrustedContext, artifact_id: str
    ) -> ArtifactRecord:
        artifact = session.scalar(
            select(ArtifactRecord).where(
                ArtifactRecord.id == artifact_id,
                ArtifactRecord.tenant_id == context.tenant_id,
                ArtifactRecord.subject_id == context.subject_id,
            )
        )
        if artifact is None:
            raise ResourceNotFound("artifact")
        return artifact

    @staticmethod
    def _artifact_snapshot(row: ArtifactRecord) -> ArtifactSnapshot:
        return ArtifactSnapshot(
            row.id, row.media_type, row.size_bytes, row.sha256, row.source_action_id, row.created_at
        )

    @staticmethod
    def _file_snapshot(row: WorkspaceEntryRecord) -> FileSnapshot:
        return FileSnapshot(
            row.workspace_id, row.logical_path, row.revision, row.etag, row.artifact_id
        )

    @staticmethod
    def _snapshot_from_body(body: dict[str, Any]) -> FileSnapshot:
        return FileSnapshot(
            str(body["workspace_id"]),
            str(body["path"]),
            int(body["revision"]),
            str(body["etag"]),
            str(body["artifact_id"]),
        )

    @staticmethod
    def _advisory_lock(
        session: Session,
        context: TrustedContext,
        method: str,
        route: str,
        key: str,
    ) -> None:
        session.execute(
            select(
                func.pg_advisory_xact_lock(
                    _lock_key(context.tenant_id, context.subject_id, method, route, key)
                )
            )
        )

    @staticmethod
    def _idempotency(
        session: Session,
        context: TrustedContext,
        method: str,
        route: str,
        key: str,
        fingerprint: str,
    ) -> IdempotencyRecord | None:
        row = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == context.tenant_id,
                IdempotencyRecord.subject_id == context.subject_id,
                IdempotencyRecord.method == method,
                IdempotencyRecord.canonical_route == route,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if row is not None and row.request_hash != fingerprint:
            raise IdempotencyConflict()
        return row

    @staticmethod
    def _record_idempotency(
        session: Session,
        context: TrustedContext,
        method: str,
        route: str,
        key: str,
        fingerprint: str,
        resource_id: str,
        location: str,
        body: dict[str, Any],
        status: int,
    ) -> None:
        session.add(
            IdempotencyRecord(
                id=_identifier("idem"),
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                method=method,
                canonical_route=route,
                idempotency_key=key,
                request_hash=fingerprint,
                response_status=status,
                resource_id=resource_id,
                location=location,
                response_body=json.loads(json.dumps(body)),
            )
        )
