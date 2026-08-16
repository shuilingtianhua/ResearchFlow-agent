import asyncio
import time
from pathlib import Path

import pytest
from anyio import sleep
from fastapi.testclient import TestClient

from researchflow.adapters.capabilities import FakeCapability
from researchflow.adapters.persistence import InMemoryRunStore, SQLiteRunStore
from researchflow.api import create_app
from researchflow.bootstrap import build_application
from researchflow.capabilities import CapabilityRegistry, CapabilityRequest, CapabilityResult
from researchflow.domain.event import EventDraft, RunEventKind
from researchflow.domain.plan import PlanDefinition, TaskSpec, TaskStatus
from researchflow.domain.run import RunSnapshot, RunStatus
from researchflow.planning import FixedResearchPlanner
from researchflow.runtime import RuntimeService
from researchflow.settings import Settings


def build_test_client(tmp_path: Path) -> TestClient:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'researchflow.db').as_posix()}"
    return TestClient(build_application(Settings(environment="test", database_url=database_url)))


def wait_for_status(
    client: TestClient,
    run_id: str,
    expected_status: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}")
        if response.json()["status"] == expected_status:
            return response.json()
        time.sleep(0.01)
    raise AssertionError(f"run {run_id!r} did not reach {expected_status!r}")


def wait_for_task_status(
    client: TestClient,
    run_id: str,
    task_id: str,
    expected_status: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["task_statuses"].get(task_id) == expected_status:
            return run
        time.sleep(0.01)
    raise AssertionError(f"task {task_id!r} did not reach {expected_status!r}")


def wait_for_event(client: TestClient, run_id: str, event_kind: str) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        events = client.get(f"/runs/{run_id}/events").json()
        if any(event["kind"] == event_kind for event in events):
            return
        time.sleep(0.01)
    raise AssertionError(f"run {run_id!r} did not emit {event_kind!r}")


class DelayedCapability(FakeCapability):
    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        await sleep(0.5)
        return await super().invoke(request)


class CloseTrackingStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_run_submission_does_not_wait_for_capability_completion() -> None:
    runtime = RuntimeService(
        store=InMemoryRunStore(),
        planner=FixedResearchPlanner(),
        capabilities=CapabilityRegistry(
            (
                DelayedCapability("librarian"),
                FakeCapability("coder"),
                FakeCapability("reporter"),
            )
        ),
    )

    with TestClient(create_app(runtime, Settings(environment="test"))) as client:
        started_at = time.monotonic()
        response = client.post(
            "/runs",
            json={"run_id": "background-run", "goal": "Reproduce a paper"},
        )
        elapsed = time.monotonic() - started_at

        assert response.status_code == 201
        assert response.json()["status"] == "running"
        assert elapsed < 0.25


@pytest.mark.parametrize(
    ("action", "run_status", "task_status"),
    (("pause", "paused", "ready"), ("cancel", "cancelled", "cancelled")),
)
def test_background_action_invalidates_late_result(
    action: str,
    run_status: str,
    task_status: str,
) -> None:
    runtime = RuntimeService(
        store=InMemoryRunStore(),
        planner=FixedResearchPlanner(),
        capabilities=CapabilityRegistry(
            (
                DelayedCapability("librarian"),
                FakeCapability("coder"),
                FakeCapability("reporter"),
            )
        ),
    )

    with TestClient(create_app(runtime, Settings(environment="test"))) as client:
        response = client.post(
            "/runs",
            json={"run_id": f"{action}-run", "goal": "Reproduce a paper"},
        )
        run_id = response.json()["run_id"]
        wait_for_task_status(client, run_id, "collect_sources", "running")

        action_response = client.post(
            f"/runs/{run_id}/{action}",
            json={"reason": "test_requested"},
        )

        assert action_response.status_code == 200
        assert action_response.json()["status"] == run_status
        assert action_response.json()["task_statuses"]["collect_sources"] == task_status
        wait_for_event(client, run_id, "task.result_ignored")
        assert client.get(f"/runs/{run_id}").json()["task_outputs"] == {}


def test_sse_stream_follows_persisted_events_and_resumes_after_sequence() -> None:
    runtime = RuntimeService(
        store=InMemoryRunStore(),
        planner=FixedResearchPlanner(),
        capabilities=CapabilityRegistry(
            (
                DelayedCapability("librarian"),
                FakeCapability("coder"),
                FakeCapability("reporter"),
            )
        ),
    )

    with TestClient(create_app(runtime, Settings(environment="test"))) as client:
        started = client.post(
            "/runs",
            json={"run_id": "stream-run", "goal": "Reproduce a paper"},
        )
        assert started.json()["status"] == "running"

        with client.stream("GET", "/runs/stream-run/events/stream") as response:
            body = response.read().decode("utf-8")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: run.started\n" in body
        assert "event: task.succeeded\n" in body
        assert "event: run.succeeded\n" in body

        history = client.get("/runs/stream-run/events").json()
        after_sequence = history[-2]["sequence"]
        with client.stream(
            "GET",
            "/runs/stream-run/events/stream",
            params={"after_sequence": after_sequence},
        ) as resumed:
            resumed_body = resumed.read().decode("utf-8")

        assert f"id: {history[-1]['sequence']}\n" in resumed_body
        assert f"id: {after_sequence}\n" not in resumed_body
        assert resumed_body.count("event: ") == 1


def test_health_endpoint(tmp_path: Path) -> None:
    with build_test_client(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ResearchFlow Agent",
        "environment": "test",
    }


def test_application_lifespan_closes_runtime_store() -> None:
    store = CloseTrackingStore()
    runtime = RuntimeService(
        store=store,
        planner=FixedResearchPlanner(),
        capabilities=CapabilityRegistry(),
    )

    with TestClient(create_app(runtime, Settings(environment="test"))) as client:
        assert client.get("/health").status_code == 200

    assert store.closed


def test_run_api_executes_and_exposes_events(tmp_path: Path) -> None:
    with build_test_client(tmp_path) as client:
        started = client.post("/runs", json={"goal": "Reproduce a paper"})

        assert started.status_code == 201
        run_id = started.json()["run_id"]
        run = wait_for_status(client, run_id, "succeeded")

        assert run["task_statuses"] == {
            "collect_sources": "succeeded",
            "prepare_experiment": "succeeded",
            "write_report": "succeeded",
        }
        assert run["task_outputs"]["write_report"]["capability"] == "reporter"

        fetched = client.get(f"/runs/{run_id}")
        assert fetched.status_code == 200
        assert fetched.json() == run

        events = client.get(f"/runs/{run_id}/events").json()
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert events[0]["kind"] == "run.created"
        assert events[-1]["kind"] == "run.succeeded"

        filtered = client.get(
            f"/runs/{run_id}/events", params={"after_sequence": events[-2]["sequence"]}
        )
        assert [event["kind"] for event in filtered.json()] == ["run.succeeded"]


def test_run_api_maps_domain_errors(tmp_path: Path) -> None:
    with build_test_client(tmp_path) as client:
        missing = client.get("/runs/not-found")
        assert missing.status_code == 404
        assert missing.json()["error"] == "NotFoundError"

        started = client.post("/runs", json={"goal": "Reproduce a paper"}).json()
        wait_for_status(client, started["run_id"], "succeeded")
        invalid_pause = client.post(
            f"/runs/{started['run_id']}/pause",
            json={"reason": "too_late"},
        )
        assert invalid_pause.status_code == 422
        assert invalid_pause.json()["error"] == "ContractViolation"


def test_run_api_rejects_duplicate_run_id(tmp_path: Path) -> None:
    with build_test_client(tmp_path) as client:
        request = {"run_id": "known-run", "goal": "Reproduce a paper"}

        assert client.post("/runs", json=request).status_code == 201
        duplicate = client.post("/runs", json=request)

        assert duplicate.status_code == 409
        assert duplicate.json()["error"] == "ConflictError"


def test_run_api_persists_across_application_rebuild(tmp_path: Path) -> None:
    with build_test_client(tmp_path) as first:
        started = first.post(
            "/runs",
            json={"run_id": "persistent-run", "goal": "Reproduce a paper"},
        )
        assert started.status_code == 201
        completed = wait_for_status(first, "persistent-run", "succeeded")

    with build_test_client(tmp_path) as rebuilt:
        fetched = rebuilt.get("/runs/persistent-run")

        assert fetched.status_code == 200
        assert fetched.json() == completed


def test_application_startup_recovers_interrupted_run_and_resume_continues(
    tmp_path: Path,
) -> None:
    database = tmp_path / "researchflow.db"
    url = f"sqlite+aiosqlite:///{database.as_posix()}"
    snapshot = RunSnapshot(
        run_id="interrupted-run",
        goal="Resume interrupted research",
        status=RunStatus.RUNNING,
        plan=PlanDefinition(
            plan_id="recovery-plan",
            tasks=(
                TaskSpec(task_id="completed", title="Completed", capability="librarian"),
                TaskSpec(
                    task_id="interrupted",
                    title="Interrupted",
                    capability="coder",
                    dependencies=("completed",),
                    max_attempts=2,
                ),
            ),
        ),
        task_statuses={
            "completed": TaskStatus.SUCCEEDED,
            "interrupted": TaskStatus.RUNNING,
        },
        task_outputs={"completed": {"summary": "preserved"}},
        task_attempts={"completed": 1, "interrupted": 1},
        task_execution_ids={"interrupted": "old-execution"},
    )

    async def seed_interrupted_run() -> None:
        store = SQLiteRunStore(url)
        await store.create(
            snapshot,
            (
                EventDraft(
                    kind=RunEventKind.TASK_SUCCEEDED,
                    run_id=snapshot.run_id,
                    task_id="completed",
                    execution_id="completed-execution",
                ),
                EventDraft(
                    kind=RunEventKind.TASK_STARTED,
                    run_id=snapshot.run_id,
                    task_id="interrupted",
                    execution_id="old-execution",
                ),
            ),
        )
        await store.close()

    asyncio.run(seed_interrupted_run())

    settings = Settings(environment="test", database_url=url)
    with TestClient(build_application(settings)) as client:
        recovered = client.get(f"/runs/{snapshot.run_id}")
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "paused"
        assert recovered.json()["task_statuses"] == {
            "completed": "succeeded",
            "interrupted": "ready",
        }

    with TestClient(build_application(settings)) as client:
        recovered_again = client.get(f"/runs/{snapshot.run_id}")
        assert recovered_again.json() == recovered.json()
        recovery_events = client.get(f"/runs/{snapshot.run_id}/events").json()
        assert [event["kind"] for event in recovery_events].count("run.recovered") == 1

        resumed = client.post(f"/runs/{snapshot.run_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "running"
        completed = wait_for_status(client, snapshot.run_id, "succeeded")
        assert completed["task_outputs"]["completed"] == {"summary": "preserved"}
        assert completed["task_attempts"] == {"completed": 1, "interrupted": 2}

        events = client.get(f"/runs/{snapshot.run_id}/events").json()
        assert [event["kind"] for event in events].count("run.recovered") == 1
        completed_starts = [
            event
            for event in events
            if event["kind"] == "task.started" and event["task_id"] == "completed"
        ]
        assert completed_starts == []
        interrupted_starts = [
            event
            for event in events
            if event["kind"] == "task.started" and event["task_id"] == "interrupted"
        ]
        assert len(interrupted_starts) == 2
        assert interrupted_starts[0]["execution_id"] == "old-execution"
        assert interrupted_starts[-1]["execution_id"] != "old-execution"
