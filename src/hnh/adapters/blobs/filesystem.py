from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from hnh.ports.execution import BlobReceipt

STORAGE_KEY = re.compile(r"^sha256/[0-9a-f]{64}$")


class FileBlobStore:
    """Immutable content-addressed blobs with atomic publication by rename."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.blobs = self.root / "blobs"
        self.temporary = self.root / "tmp"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.temporary.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> BlobReceipt:
        digest = hashlib.sha256(content).hexdigest()
        storage_key = f"sha256/{digest}"
        target = self.blobs / digest
        descriptor, temporary_name = tempfile.mkstemp(prefix="blob-", dir=self.temporary)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                temporary_path.unlink()
            else:
                os.replace(temporary_path, target)
                directory_fd = os.open(self.blobs, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return BlobReceipt(storage_key, digest, len(content))

    def get(self, storage_key: str) -> bytes:
        return self._path(storage_key).read_bytes()

    def list_keys(self) -> set[str]:
        return {
            f"sha256/{path.name}"
            for path in self.blobs.iterdir()
            if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name)
        }

    def delete(self, storage_key: str) -> None:
        self._path(storage_key).unlink(missing_ok=True)

    def _path(self, storage_key: str) -> Path:
        if STORAGE_KEY.fullmatch(storage_key) is None:
            raise ValueError("invalid blob storage key")
        return self.blobs / storage_key.removeprefix("sha256/")
