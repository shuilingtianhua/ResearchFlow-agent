import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from researchflow.adapters.persistence import SQLiteRunStore
from researchflow.domain.artifact import ArtifactRef
from researchflow.domain.errors import ConflictError, ContractViolation, DependencyUnavailable
from researchflow.domain.event import EventDraft
from researchflow.domain.plan import PlanDefinition, TaskSpec, TaskStatus
from researchflow.domain.run import RunSnapshot, RunStatus


def database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def populated_snapshot() -> RunSnapshot:
    plan = PlanDefinition(
        plan_id="plan-1",
        tasks=(TaskSpec(task_id="read", title="Read paper", capability="librarian"),),
    )
    return RunSnapshot(
        run_id="run-1",
        goal="Reproduce a paper",
        status=RunStatus.RUNNING,
        version=1,
        plan=plan,
        task_statuses={"read": TaskStatus.SUCCEEDED},
        task_outputs={"read": {"summary": "source collected"}},
        task_artifacts={
            "read": (
                ArtifactRef(
                    artifact_id="source",
                    kind="text/plain",
                    uri="artifact://sha256/" + "a" * 64,
                    sha256="a" * 64,
                    producer_task_id="read",
                ),
            )
        },
        task_attempts={"read": 1},
    )


def test_sqlite_store_persists_snapshot_and_events_across_instances(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = database_url(tmp_path / "researchflow.db")
        first = SQLiteRunStore(url)
        initial = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        await first.create(initial, (EventDraft(kind="run.created", run_id="run-1"),))
        expected, _ = await first.commit(
            populated_snapshot(),
            (EventDraft(kind="run.started", run_id="run-1", payload={"source": "api"}),),
            expected_version=0,
        )
        await first.close()

        reopened = SQLiteRunStore(url)
        assert await reopened.load("run-1") == expected
        events = await reopened.list_events("run-1")
        assert [event.sequence for event in events] == [1, 2]
        assert [event.kind for event in events] == ["run.created", "run.started"]
        assert events[-1].payload == {"source": "api"}
        assert [event.sequence for event in await reopened.list_events("run-1", 1)] == [2]
        await reopened.close()

    asyncio.run(scenario())


def test_sqlite_store_lists_matching_snapshots_after_reopen(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = database_url(tmp_path / "researchflow.db")
        first = SQLiteRunStore(url)
        running = replace(populated_snapshot(), version=0)
        completed = replace(
            running,
            run_id="completed-run",
            status=RunStatus.SUCCEEDED,
        )
        await first.create(running)
        await first.create(completed)
        await first.close()

        reopened = SQLiteRunStore(url)
        matches = await reopened.list_by_status(frozenset({RunStatus.RUNNING}))

        assert matches == (running,)
        await reopened.close()

    asyncio.run(scenario())


def test_sqlite_store_rejects_a_stale_version(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteRunStore(database_url(tmp_path / "researchflow.db"))
        snapshot = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        await store.create(snapshot)
        await store.commit(replace(snapshot, version=1), (), expected_version=0)

        with pytest.raises(ConflictError, match="version changed"):
            await store.commit(replace(snapshot, version=1), (), expected_version=0)
        await store.close()

    asyncio.run(scenario())


def test_sqlite_store_allows_only_one_concurrent_version_winner(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = database_url(tmp_path / "researchflow.db")
        first = SQLiteRunStore(url)
        second = SQLiteRunStore(url)
        snapshot = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        await first.create(snapshot)
        await second.load("run-1")
        next_snapshot = replace(snapshot, status=RunStatus.RUNNING, version=1)

        results = await asyncio.gather(
            first.commit(
                next_snapshot,
                (EventDraft(kind="run.started", run_id="run-1", payload={"writer": 1}),),
                expected_version=0,
            ),
            second.commit(
                next_snapshot,
                (EventDraft(kind="run.started", run_id="run-1", payload={"writer": 2}),),
                expected_version=0,
            ),
            return_exceptions=True,
        )

        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, ConflictError) for result in results) == 1
        assert (await first.load("run-1")).version == 1
        assert len(await first.list_events("run-1")) == 1
        await first.close()
        await second.close()

    asyncio.run(scenario())


def test_sqlite_store_rolls_back_snapshot_when_an_event_is_invalid(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteRunStore(database_url(tmp_path / "researchflow.db"))
        snapshot = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        await store.create(snapshot)

        with pytest.raises(ContractViolation, match="run_id"):
            await store.commit(
                replace(snapshot, version=1),
                (
                    EventDraft(kind="run.started", run_id="run-1"),
                    EventDraft(kind="run.started", run_id="another-run"),
                ),
                expected_version=0,
            )

        assert await store.list_events("run-1") == ()
        assert (await store.load("run-1")).version == 0
        await store.close()

    asyncio.run(scenario())


def test_sqlite_store_rolls_back_snapshot_when_event_serialization_fails(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = SQLiteRunStore(database_url(tmp_path / "researchflow.db"))
        snapshot = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        await store.create(snapshot)

        with pytest.raises(ContractViolation, match="JSON-serializable"):
            await store.commit(
                replace(snapshot, version=1),
                (EventDraft(kind="run.started", run_id="run-1", payload={"bad": object()}),),
                expected_version=0,
            )

        assert await store.list_events("run-1") == ()
        assert (await store.load("run-1")).version == 0
        await store.close()

    asyncio.run(scenario())


def test_sqlite_store_rejects_a_duplicate_run_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteRunStore(database_url(tmp_path / "researchflow.db"))
        snapshot = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        await store.create(snapshot)

        with pytest.raises(ConflictError, match="already exists"):
            await store.create(snapshot)
        await store.close()

    asyncio.run(scenario())


def test_sqlite_store_reports_a_corrupt_database(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "researchflow.db"
        database.write_text("not a sqlite database", encoding="utf-8")
        store = SQLiteRunStore(database_url(database))

        with pytest.raises(DependencyUnavailable, match="SQLite"):
            await store.load("run-1")
        await store.close()

    asyncio.run(scenario())


def test_sqlite_store_reports_a_corrupt_snapshot_during_status_listing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "researchflow.db"
        store = SQLiteRunStore(database_url(database))
        await store.create(RunSnapshot(run_id="run-1", goal="Reproduce a paper"))
        await store.close()

        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE runs SET snapshot_json = ? WHERE run_id = ?",
                ("not-json", "run-1"),
            )

        reopened = SQLiteRunStore(database_url(database))
        with pytest.raises(DependencyUnavailable, match="stored run snapshot is invalid"):
            await reopened.list_by_status(frozenset({RunStatus.RUNNING}))
        await reopened.close()

    asyncio.run(scenario())
