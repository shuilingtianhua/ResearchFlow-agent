import asyncio
from dataclasses import replace

import pytest

from researchflow.adapters.persistence import InMemoryRunStore
from researchflow.domain.errors import ConflictError, ContractViolation
from researchflow.domain.event import EventDraft
from researchflow.domain.run import RunSnapshot, RunStatus


def test_store_commits_snapshot_and_events_atomically() -> None:
    async def scenario() -> None:
        store = InMemoryRunStore()
        snapshot = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        _, created_events = await store.create(
            snapshot, (EventDraft(kind="run.created", run_id="run-1"),)
        )
        running = replace(snapshot, status=RunStatus.RUNNING, version=1)
        stored, running_events = await store.commit(
            running,
            (EventDraft(kind="run.started", run_id="run-1"),),
            expected_version=0,
        )

        assert stored.version == 1
        assert created_events[0].sequence == 1
        assert running_events[0].sequence == 2
        assert [event.kind for event in await store.list_events("run-1")] == [
            "run.created",
            "run.started",
        ]

    asyncio.run(scenario())


def test_store_rejects_a_stale_version() -> None:
    async def scenario() -> None:
        store = InMemoryRunStore()
        snapshot = RunSnapshot(run_id="run-1", goal="Reproduce a paper")
        await store.create(snapshot)
        await store.commit(replace(snapshot, version=1), (), expected_version=0)

        with pytest.raises(ConflictError, match="version changed"):
            await store.commit(replace(snapshot, version=1), (), expected_version=0)

    asyncio.run(scenario())


def test_store_does_not_partially_append_invalid_events() -> None:
    async def scenario() -> None:
        store = InMemoryRunStore()
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

    asyncio.run(scenario())
