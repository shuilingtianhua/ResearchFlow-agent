import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from researchflow.adapters.persistence import SQLiteRunStore
from researchflow.bootstrap import build_application
from researchflow.domain.event import EventDraft, RunEventKind
from researchflow.domain.plan import PlanDefinition, TaskSpec, TaskStatus
from researchflow.domain.run import RunSnapshot, RunStatus
from researchflow.settings import Settings


def build_test_client(tmp_path: Path) -> TestClient:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'researchflow.db').as_posix()}"
    return TestClient(build_application(Settings(environment="test", database_url=database_url)))


def test_health_endpoint(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ResearchFlow Agent",
        "environment": "test",
    }


def test_run_api_executes_and_exposes_events(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)

    started = client.post("/runs", json={"goal": "Reproduce a paper"})

    assert started.status_code == 201
    run = started.json()
    assert run["status"] == "succeeded"
    assert run["task_statuses"] == {
        "collect_sources": "succeeded",
        "prepare_experiment": "succeeded",
        "write_report": "succeeded",
    }
    assert run["task_outputs"]["write_report"]["capability"] == "reporter"

    fetched = client.get(f"/runs/{run['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == run

    events = client.get(f"/runs/{run['run_id']}/events").json()
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["kind"] == "run.created"
    assert events[-1]["kind"] == "run.succeeded"

    filtered = client.get(
        f"/runs/{run['run_id']}/events", params={"after_sequence": events[-2]["sequence"]}
    )
    assert [event["kind"] for event in filtered.json()] == ["run.succeeded"]


def test_run_api_maps_domain_errors(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)

    missing = client.get("/runs/not-found")
    assert missing.status_code == 404
    assert missing.json()["error"] == "NotFoundError"

    started = client.post("/runs", json={"goal": "Reproduce a paper"}).json()
    invalid_pause = client.post(
        f"/runs/{started['run_id']}/pause",
        json={"reason": "too_late"},
    )
    assert invalid_pause.status_code == 422
    assert invalid_pause.json()["error"] == "ContractViolation"


def test_run_api_rejects_duplicate_run_id(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)
    request = {"run_id": "known-run", "goal": "Reproduce a paper"}

    assert client.post("/runs", json=request).status_code == 201
    duplicate = client.post("/runs", json=request)

    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "ConflictError"


def test_run_api_persists_across_application_rebuild(tmp_path: Path) -> None:
    first = build_test_client(tmp_path)
    started = first.post(
        "/runs",
        json={"run_id": "persistent-run", "goal": "Reproduce a paper"},
    )
    assert started.status_code == 201

    rebuilt = build_test_client(tmp_path)
    fetched = rebuilt.get("/runs/persistent-run")

    assert fetched.status_code == 200
    assert fetched.json() == started.json()


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
        assert resumed.json()["status"] == "succeeded"
        assert resumed.json()["task_outputs"]["completed"] == {"summary": "preserved"}
        assert resumed.json()["task_attempts"] == {"completed": 1, "interrupted": 2}

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
