from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactStore:
    """Content-addressed local artifact store with write-before-reference semantics."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:]

    def put_bytes(self, data: bytes) -> str:
        digest = self.digest_bytes(data)
        target = self.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self.verify(digest)
            return digest
        temp = target.with_suffix(".tmp")
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        self.verify(digest)
        return digest

    def get_bytes(self, digest: str) -> bytes:
        path = self.path_for(digest)
        if not path.exists():
            raise ArtifactIntegrityError(f"missing artifact: {digest}")
        data = path.read_bytes()
        actual = self.digest_bytes(data)
        if actual != digest:
            raise ArtifactIntegrityError(
                f"corrupt artifact: expected {digest}, observed {actual}"
            )
        return data

    def verify(self, digest: str) -> None:
        self.get_bytes(digest)
