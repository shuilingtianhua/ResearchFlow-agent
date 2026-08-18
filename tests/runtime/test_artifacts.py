import asyncio
import hashlib
from pathlib import Path

from researchflow.adapters.artifacts import FilesystemArtifactStore
from researchflow.adapters.persistence import InMemoryRunStore
from researchflow.artifacts import ArtifactPayload
from researchflow.capabilities import CapabilityRegistry, CapabilityRequest, CapabilityResult
from researchflow.domain.artifact import ArtifactRef
from researchflow.domain.event import RunEventKind
from researchflow.domain.plan import PlanDefinition, TaskSpec, TaskStatus
from researchflow.planning import PlanningContext
from researchflow.runtime import RuntimeService, StartRun


class ArtifactPlanner:
    async def build(self, context: PlanningContext) -> PlanDefinition:
        return PlanDefinition(
            plan_id=f"{context.run_id}-plan",
            tasks=(
                TaskSpec(task_id="source", title="Source", capability="source"),
                TaskSpec(
                    task_id="consumer",
                    title="Consumer",
                    capability="consumer",
                    dependencies=("source",),
                ),
            ),
        )


class ProducingCapability:
    name = "source"

    def __init__(self, store: FilesystemArtifactStore) -> None:
        self._store = store

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        artifact = await self._store.put(
            ArtifactPayload(
                artifact_id="source-text",
                kind="text/plain",
                chunks=_chunks(b"source bytes"),
                producer_task_id=request.task_id,
            )
        )
        return CapabilityResult(artifacts=(artifact,))


class ConsumingCapability:
    name = "consumer"

    def __init__(self) -> None:
        self.requests: list[CapabilityRequest] = []

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        self.requests.append(request)
        return CapabilityResult(outputs={"artifact_count": len(request.artifacts)})


class BrokenProducingCapability:
    name = "source"

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        digest = hashlib.sha256(b"missing").hexdigest()
        return CapabilityResult(
            artifacts=(
                ArtifactRef(
                    artifact_id="missing",
                    kind="text/plain",
                    uri=f"artifact://sha256/{digest}",
                    sha256=digest,
                    producer_task_id=request.task_id,
                ),
            )
        )


def _chunks(*values: bytes):
    async def stream():
        for value in values:
            yield value

    return stream()


def test_runtime_persists_verified_artifacts_and_passes_dependencies(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = FilesystemArtifactStore(tmp_path)
        consumer = ConsumingCapability()
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=ArtifactPlanner(),
            capabilities=CapabilityRegistry((ProducingCapability(store), consumer)),
            artifact_store=store,
        )

        result = await runtime.dispatch(StartRun(run_id="artifact-run", goal="Use artifacts"))

        artifact = result.snapshot.task_artifacts["source"][0]
        assert result.snapshot.status.value == "succeeded"
        assert result.snapshot.task_statuses["consumer"] == TaskStatus.SUCCEEDED
        assert consumer.requests[0].artifacts == (artifact,)
        stored_events = [
            event for event in result.emitted_events if event.kind == RunEventKind.ARTIFACT_STORED
        ]
        assert len(stored_events) == 1
        assert stored_events[0].task_id == "source"
        assert stored_events[0].payload["sha256"] == artifact.sha256

    asyncio.run(scenario())


def test_runtime_rejects_unverifiable_artifacts(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=ArtifactPlanner(),
            capabilities=CapabilityRegistry((BrokenProducingCapability(),)),
            artifact_store=FilesystemArtifactStore(tmp_path),
        )

        result = await runtime.dispatch(StartRun(run_id="broken-artifact-run", goal="Reject it"))

        assert result.snapshot.status.value == "failed"
        assert result.snapshot.task_statuses["source"] == TaskStatus.FAILED
        assert result.snapshot.task_artifacts == {}
        assert not any(
            event.kind == RunEventKind.ARTIFACT_STORED for event in result.emitted_events
        )

    asyncio.run(scenario())
