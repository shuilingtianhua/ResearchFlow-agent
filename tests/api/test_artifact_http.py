import time
from pathlib import Path

from fastapi.testclient import TestClient

from researchflow.adapters.artifacts import FilesystemArtifactStore
from researchflow.adapters.persistence import InMemoryRunStore
from researchflow.artifacts import ArtifactPayload
from researchflow.capabilities import CapabilityRegistry, CapabilityRequest, CapabilityResult
from researchflow.domain.plan import PlanDefinition, TaskSpec
from researchflow.planning import PlanningContext
from researchflow.runtime import RuntimeService
from researchflow.settings import Settings


class SourcePlanner:
    async def build(self, context: PlanningContext) -> PlanDefinition:
        return PlanDefinition(
            plan_id=f"{context.run_id}-plan",
            tasks=(TaskSpec(task_id="source", title="Source", capability="source"),),
        )


class SourceCapability:
    name = "source"

    def __init__(self, store: FilesystemArtifactStore) -> None:
        self._store = store

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        artifact = await self._store.put(
            ArtifactPayload(
                artifact_id="source-text",
                kind="text/plain",
                chunks=_chunks(b"downloadable source"),
                producer_task_id=request.task_id,
            )
        )
        return CapabilityResult(artifacts=(artifact,))


def _chunks(*values: bytes):
    async def stream():
        for value in values:
            yield value

    return stream()


def _client(tmp_path: Path) -> tuple[TestClient, FilesystemArtifactStore]:
    artifact_store = FilesystemArtifactStore(tmp_path)
    runtime = RuntimeService(
        store=InMemoryRunStore(),
        planner=SourcePlanner(),
        capabilities=CapabilityRegistry((SourceCapability(artifact_store),)),
        artifact_store=artifact_store,
    )
    from researchflow.api import create_app

    return TestClient(create_app(runtime, Settings(environment="test"))), artifact_store


def _wait_for_succeeded(client: TestClient, run_id: str) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if client.get(f"/runs/{run_id}").json()["status"] == "succeeded":
            return
        time.sleep(0.01)
    raise AssertionError("run did not complete")


def test_artifact_metadata_and_content_are_scoped_to_run(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        response = client.post("/runs", json={"run_id": "artifact-http", "goal": "Download"})
        assert response.status_code == 201
        _wait_for_succeeded(client, "artifact-http")

        metadata = client.get("/runs/artifact-http/artifacts/source-text")
        assert metadata.status_code == 200
        assert metadata.json()["artifact_id"] == "source-text"
        assert metadata.json()["sha256"]
        assert "downloadable source" not in metadata.text

        content = client.get("/runs/artifact-http/artifacts/source-text/content")
        assert content.status_code == 200
        assert content.headers["content-type"] == "text/plain; charset=utf-8"
        assert content.content == b"downloadable source"

        missing = client.get("/runs/artifact-http/artifacts/not-found")
        assert missing.status_code == 404


def test_artifact_download_rejects_tampered_content(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        assert (
            client.post("/runs", json={"run_id": "tampered-http", "goal": "Reject"}).status_code
            == 201
        )
        _wait_for_succeeded(client, "tampered-http")
        artifact = client.get("/runs/tampered-http").json()["task_artifacts"]["source"][0]
        content_path = next(tmp_path.rglob(artifact["sha256"]))
        content_path.write_bytes(b"tampered")

        response = client.get("/runs/tampered-http/artifacts/source-text/content")
        assert response.status_code == 503
