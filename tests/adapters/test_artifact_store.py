import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from researchflow.adapters.artifacts import FilesystemArtifactStore
from researchflow.artifacts import ArtifactPayload
from researchflow.domain.artifact import ArtifactRef
from researchflow.domain.errors import ContractViolation, NotFoundError


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def test_filesystem_store_puts_stream_and_verifies_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = FilesystemArtifactStore(tmp_path)
        payload = ArtifactPayload(
            artifact_id="paper-source",
            kind="text/plain",
            chunks=chunks(b"hello", b" world"),
            producer_task_id="collect_sources",
        )

        artifact = await store.put(payload)
        expected_digest = hashlib.sha256(b"hello world").hexdigest()

        assert artifact.sha256 == expected_digest
        assert artifact.uri == f"artifact://sha256/{expected_digest}"
        assert artifact.producer_task_id == "collect_sources"
        assert b"".join([chunk async for chunk in store.open(artifact)]) == b"hello world"
        assert await store.verify(artifact)

    asyncio.run(scenario())


def test_filesystem_store_deduplicates_by_content_digest(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = FilesystemArtifactStore(tmp_path)
        first = await store.put(
            ArtifactPayload("first", "text", chunks(b"same"), metadata={"version": 1})
        )
        second = await store.put(
            ArtifactPayload("second", "text", chunks(b"same"), metadata={"version": 2})
        )
        different = await store.put(ArtifactPayload("first", "text", chunks(b"changed")))

        assert first.uri == second.uri
        assert first.sha256 == second.sha256
        assert different.uri != first.uri
        assert sorted(path.name for path in tmp_path.rglob("*") if path.is_file()) == sorted(
            (first.sha256, different.sha256)
        )

    asyncio.run(scenario())


def test_filesystem_store_detects_mutation_and_missing_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = FilesystemArtifactStore(tmp_path)
        artifact = await store.put(ArtifactPayload("source", "text", chunks(b"original")))
        content_path = next(path for path in tmp_path.rglob(artifact.sha256))
        content_path.write_bytes(b"tampered")

        assert not await store.verify(artifact)
        with pytest.raises(NotFoundError):
            missing = ArtifactRef(
                artifact_id="missing",
                kind="text",
                uri="artifact://sha256/" + "0" * 64,
                sha256="0" * 64,
            )
            b"".join([chunk async for chunk in store.open(missing)])

    asyncio.run(scenario())


def test_filesystem_store_rejects_untrusted_artifact_uri(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = FilesystemArtifactStore(tmp_path)
        artifact = await store.put(ArtifactPayload("source", "text", chunks(b"content")))
        invalid = ArtifactRef(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            uri="file:///outside/root",
            sha256=artifact.sha256,
        )

        assert not await store.verify(invalid)
        with pytest.raises(ContractViolation):
            b"".join([chunk async for chunk in store.open(invalid)])

    asyncio.run(scenario())
