from pathlib import Path

from fastapi.testclient import TestClient

from researchflow.bootstrap import build_application
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
