import asyncio
from pathlib import Path

from researchflow.adapters.capabilities import FakeCapability
from researchflow.adapters.persistence import SQLiteRunStore
from researchflow.capabilities import CapabilityRegistry, CapabilityRequest, CapabilityResult
from researchflow.domain.event import RunEventKind
from researchflow.domain.plan import PlanDefinition, TaskSpec, TaskStatus
from researchflow.domain.run import RunBudget, RunSnapshot, RunStatus
from researchflow.planning import FixedResearchPlanner, PlanningContext
from researchflow.runtime import RuntimeService, StartRun


class DelayedCapability(FakeCapability):
    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        await asyncio.sleep(0.1)
        return await super().invoke(request)


class BlockingCapability(FakeCapability):
    def __init__(self, name: str, expected_starts: int = 1) -> None:
        super().__init__(name)
        self._expected_starts = expected_starts
        self.started = asyncio.Event()

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        self.invocations.append(request)
        if len(self.invocations) == self._expected_starts:
            self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking capability should be cancelled")


class ParallelPlanner:
    def __init__(self, capability_name: str, task_count: int = 2) -> None:
        self._capability_name = capability_name
        self._task_count = task_count

    async def build(self, context: PlanningContext) -> PlanDefinition:
        return PlanDefinition(
            plan_id=f"{context.run_id}-parallel-plan",
            tasks=tuple(
                TaskSpec(
                    task_id=f"parallel_{index}",
                    title=f"Parallel task {index}",
                    capability=self._capability_name,
                )
                for index in range(self._task_count)
            ),
        )


async def wait_for_run_status(
    store: SQLiteRunStore,
    run_id: str,
    expected_status: RunStatus,
) -> RunSnapshot:
    for _ in range(200):
        snapshot = await store.load(run_id)
        if snapshot.status == expected_status:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id!r} did not reach {expected_status.value!r}")


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


def test_competing_workers_execute_each_task_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'researchflow.db').as_posix()}"
        first_store = SQLiteRunStore(url)
        second_store = SQLiteRunStore(url)
        librarian = DelayedCapability("librarian")
        coder = FakeCapability("coder")
        reporter = FakeCapability("reporter")
        capabilities = CapabilityRegistry((librarian, coder, reporter))
        first_runtime = RuntimeService(first_store, FixedResearchPlanner(), capabilities)
        second_runtime = RuntimeService(second_store, FixedResearchPlanner(), capabilities)
        await first_store.list_by_status(frozenset())
        await second_store.list_by_status(frozenset())
        workers = (
            asyncio.create_task(first_runtime.run_worker()),
            asyncio.create_task(second_runtime.run_worker()),
        )

        submitted = await first_runtime.submit(
            StartRun(run_id="background-run", goal="Reproduce a paper")
        )
        completed = await wait_for_run_status(
            first_store,
            submitted.snapshot.run_id,
            RunStatus.SUCCEEDED,
        )

        assert completed.status == RunStatus.SUCCEEDED
        assert len(librarian.invocations) == 1
        assert len(coder.invocations) == 1
        assert len(reporter.invocations) == 1
        assert [invocation.lease_epoch for invocation in librarian.invocations] == [1]
        assert [invocation.lease_epoch for invocation in coder.invocations] == [1]
        assert [invocation.lease_epoch for invocation in reporter.invocations] == [1]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await first_store.close()
        await second_store.close()

    asyncio.run(scenario())


def test_worker_shutdown_leaves_a_recoverable_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'researchflow.db').as_posix()}"
        store = SQLiteRunStore(url)
        capability = BlockingCapability("librarian")
        runtime = RuntimeService(
            store,
            FixedResearchPlanner(),
            CapabilityRegistry((capability, FakeCapability("coder"), FakeCapability("reporter"))),
        )
        worker = asyncio.create_task(runtime.run_worker())
        submitted = await runtime.submit(StartRun(run_id="shutdown-run", goal="Reproduce a paper"))
        await capability.started.wait()

        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        interrupted = await store.load(submitted.snapshot.run_id)
        assert interrupted.status == RunStatus.RUNNING
        assert interrupted.task_statuses["collect_sources"] == TaskStatus.RUNNING
        assert await runtime.recover_interrupted_runs() == 1
        recovered = await store.load(submitted.snapshot.run_id)
        assert recovered.status == RunStatus.PAUSED
        assert recovered.task_statuses["collect_sources"] == TaskStatus.READY
        await store.close()

    asyncio.run(scenario())


def test_competing_workers_execute_parallel_tasks_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'parallel-workers.db').as_posix()}"
        first_store = SQLiteRunStore(url)
        second_store = SQLiteRunStore(url)
        capability = DelayedCapability("parallel")
        capabilities = CapabilityRegistry((capability,))
        planner = ParallelPlanner(capability.name, task_count=3)
        first_runtime = RuntimeService(first_store, planner, capabilities)
        second_runtime = RuntimeService(second_store, planner, capabilities)
        await first_store.list_by_status(frozenset())
        await second_store.list_by_status(frozenset())
        workers = (
            asyncio.create_task(first_runtime.run_worker()),
            asyncio.create_task(second_runtime.run_worker()),
        )

        submitted = await first_runtime.submit(
            StartRun(
                run_id="parallel-workers-run",
                goal="Execute parallel tasks exactly once",
                budget=RunBudget(max_concurrency=2),
            )
        )
        completed = await wait_for_run_status(
            first_store,
            submitted.snapshot.run_id,
            RunStatus.SUCCEEDED,
        )

        assert completed.task_attempts == {
            "parallel_0": 1,
            "parallel_1": 1,
            "parallel_2": 1,
        }
        assert sorted(request.task_id for request in capability.invocations) == [
            "parallel_0",
            "parallel_1",
            "parallel_2",
        ]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await first_store.close()
        await second_store.close()

    asyncio.run(scenario())


def test_worker_shutdown_leaves_all_parallel_executions_recoverable(tmp_path: Path) -> None:
    async def scenario() -> None:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'parallel-shutdown.db').as_posix()}"
        store = SQLiteRunStore(url)
        capability = BlockingCapability("parallel", expected_starts=2)
        runtime = RuntimeService(
            store,
            ParallelPlanner(capability.name),
            CapabilityRegistry((capability,)),
        )
        worker = asyncio.create_task(runtime.run_worker())
        submitted = await runtime.submit(
            StartRun(
                run_id="parallel-shutdown-run",
                goal="Recover parallel work after shutdown",
                budget=RunBudget(max_concurrency=2),
            )
        )
        await asyncio.wait_for(capability.started.wait(), timeout=1)

        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        interrupted = await store.load(submitted.snapshot.run_id)
        assert set(interrupted.task_statuses.values()) == {TaskStatus.RUNNING}
        assert len(interrupted.task_execution_ids) == 2
        await store.close()

        recovery_store = SQLiteRunStore(url)
        recovery_runtime = RuntimeService(
            recovery_store,
            ParallelPlanner(capability.name),
            CapabilityRegistry((capability,)),
        )
        assert await recovery_runtime.recover_interrupted_runs() == 1
        recovered = await recovery_store.load(submitted.snapshot.run_id)
        assert recovered.status == RunStatus.PAUSED
        assert set(recovered.task_statuses.values()) == {TaskStatus.READY}
        assert recovered.task_execution_ids == {}
        await recovery_store.close()

    asyncio.run(scenario())
