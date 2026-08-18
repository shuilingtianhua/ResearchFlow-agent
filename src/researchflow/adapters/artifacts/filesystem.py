"""Content-addressed filesystem implementation of the ArtifactStore port."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from uuid import uuid4

from anyio import CancelScope, open_file, to_thread

from researchflow.artifacts import ArtifactPayload
from researchflow.domain.artifact import ArtifactRef
from researchflow.domain.errors import ContractViolation, NotFoundError

_CHUNK_SIZE = 1024 * 1024
_URI_PREFIX = "artifact://sha256/"


class FilesystemArtifactStore:
    """Store immutable artifact bytes beneath digest-derived paths."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    async def put(self, payload: ArtifactPayload) -> ArtifactRef:
        with CancelScope(shield=True):
            result = await self._put(payload)
        return result

    async def _put(self, payload: ArtifactPayload) -> ArtifactRef:
        await to_thread.run_sync(partial(self._root.mkdir, parents=True, exist_ok=True))
        temporary = self._root / f".upload-{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        try:
            async with await open_file(temporary, "wb") as file:
                async for chunk in payload.chunks:
                    if not isinstance(chunk, bytes):
                        raise ContractViolation("artifact chunks must be bytes")
                    digest.update(chunk)
                    await file.write(chunk)
            digest_value = digest.hexdigest()
            destination = self._path_for_digest(digest_value)
            await to_thread.run_sync(partial(destination.parent.mkdir, parents=True, exist_ok=True))
            if destination.exists():
                await to_thread.run_sync(temporary.unlink)
            else:
                await to_thread.run_sync(temporary.replace, destination)
            return ArtifactRef(
                artifact_id=payload.artifact_id,
                kind=payload.kind,
                uri=_uri_for_digest(digest_value),
                sha256=digest_value,
                producer_task_id=payload.producer_task_id,
                metadata=payload.metadata,
            )
        except BaseException:
            if temporary.exists():
                await to_thread.run_sync(temporary.unlink)
            raise

    async def verify(self, artifact: ArtifactRef) -> bool:
        with CancelScope(shield=True):
            result = await self._verify(artifact)
        return result

    async def _verify(self, artifact: ArtifactRef) -> bool:
        try:
            path = self._path_for_artifact(artifact)
        except ContractViolation:
            return False
        if not path.is_file():
            return False
        digest = hashlib.sha256()
        async with await open_file(path, "rb") as file:
            while chunk := await file.read(_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest() == artifact.sha256

    def open(self, artifact: ArtifactRef) -> AsyncIterator[bytes]:
        return self._read(artifact)

    async def _read(self, artifact: ArtifactRef) -> AsyncIterator[bytes]:
        path = self._path_for_artifact(artifact)
        if not path.is_file():
            raise NotFoundError(f"artifact {artifact.artifact_id!r} was not found")
        async with await open_file(path, "rb") as file:
            while chunk := await file.read(_CHUNK_SIZE):
                yield chunk

    def _path_for_artifact(self, artifact: ArtifactRef) -> Path:
        expected_uri = _uri_for_digest(artifact.sha256)
        if artifact.uri != expected_uri:
            raise ContractViolation("artifact URI does not match its SHA-256 digest")
        return self._path_for_digest(artifact.sha256)

    def _path_for_digest(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ContractViolation("sha256 must be a lowercase 64-character digest")
        return self._root / digest[:2] / digest


def _uri_for_digest(digest: str) -> str:
    return f"{_URI_PREFIX}{digest}"
