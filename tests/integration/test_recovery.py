import asyncio
from pathlib import Path

from researchflow.adapters.capabilities import FakeCapability
from researchflow.adapters.persistence import SQLiteRunStore
from researchflow.capabilities import CapabilityRegistry
from researchflow.domain.event import RunEventKind
from researchflow.domain.plan import PlanDefinition, TaskSpec, TaskStatus
from researchflow.domain.run import RunSnapshot, RunStatus
from researchflow.planning import FixedResearchPlanner
from researchflow.runtime import RuntimeService


def test_competing_recovery_services_commit_only_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'researchflow.db').as_posix()}"
        first_store = SQLiteRunStore(url)
        second_store = SQLiteRunStore(url)
        snapshot = RunSnapshot(
            run_id="interrupted-run",
            goal="Resume interrupted research",
            status=RunStatus.RUNNING,
            plan=PlanDefinition(
                plan_id="recovery-plan",
                tasks=(
                    TaskSpec(
                        task_id="interrupted",
                        title="Interrupted",
                        capability="worker",
                        max_attempts=2,
                    ),
                ),
            ),
            task_statuses={"interrupted": TaskStatus.RUNNING},
            task_attempts={"interrupted": 1},
            task_execution_ids={"interrupted": "old-execution"},
        )
        await first_store.create(snapshot)
        capabilities = CapabilityRegistry((FakeCapability("worker"),))
        first_runtime = RuntimeService(first_store, FixedResearchPlanner(), capabilities)
        second_runtime = RuntimeService(second_store, FixedResearchPlanner(), capabilities)

        recovered_counts = await asyncio.gather(
            first_runtime.recover_interrupted_runs(),
            second_runtime.recover_interrupted_runs(),
        )

        assert sum(recovered_counts) == 1
        recovered = await first_store.load(snapshot.run_id)
        assert recovered.status == RunStatus.PAUSED
        events = await first_store.list_events(snapshot.run_id)
        assert [event.kind for event in events] == [RunEventKind.RUN_RECOVERED]
        await first_store.close()
        await second_store.close()

    asyncio.run(scenario())
