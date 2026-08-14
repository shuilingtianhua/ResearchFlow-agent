import asyncio
from dataclasses import replace

from researchflow.adapters.capabilities import FakeCapability
from researchflow.adapters.persistence import InMemoryRunStore
from researchflow.capabilities import CapabilityRegistry, CapabilityRequest, CapabilityResult
from researchflow.domain.errors import ExecutionFailure
from researchflow.domain.event import EventDraft, RunEvent, RunEventKind
from researchflow.domain.plan import PlanDefinition, TaskSpec, TaskStatus
from researchflow.domain.run import RunBudget, RunSnapshot, RunStatus
from researchflow.planning import FixedResearchPlanner, PlanningContext
from researchflow.runtime import CancelRun, PauseRun, ResumeRun, RuntimeService, StartRun


class SingleTaskPlanner:
    def __init__(
        self,
        capability_name: str,
        *,
        max_attempts: int = 1,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._capability_name = capability_name
        self._max_attempts = max_attempts
        self._timeout_seconds = timeout_seconds

    async def build(self, context: PlanningContext) -> PlanDefinition:
        return PlanDefinition(
            plan_id=f"{context.run_id}-plan",
            tasks=(
                TaskSpec(
                    task_id="controlled_task",
                    title="Controlled task",
                    capability=self._capability_name,
                    max_attempts=self._max_attempts,
                    timeout_seconds=self._timeout_seconds,
                ),
            ),
        )


class FailingPlanner:
    async def build(self, context: PlanningContext) -> PlanDefinition:
        raise RuntimeError("planner unavailable")


class BlockingCapability:
    def __init__(self, name: str) -> None:
        self._name = name
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.invocation_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        self.invocation_count += 1
        if self.invocation_count == 1:
            self.started.set()
            await self.release.wait()
        return CapabilityResult(outputs={"invocation": self.invocation_count})


class PauseBeforeResultStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.injected = False

    async def commit(
        self,
        snapshot: RunSnapshot,
        events: tuple[EventDraft, ...],
        *,
        expected_version: int,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        result_kinds = {RunEventKind.TASK_SUCCEEDED, RunEventKind.TASK_FAILED}
        is_task_result = any(event.kind in result_kinds for event in events)
        if is_task_result and not self.injected:
            self.injected = True
            current = await self.load(snapshot.run_id)
            task_id = next(iter(current.task_execution_ids))
            statuses = dict(current.task_statuses)
            statuses[task_id] = TaskStatus.READY
            paused = replace(
                current,
                status=RunStatus.PAUSED,
                version=current.version + 1,
                task_statuses=statuses,
                task_execution_ids={},
            )
            await super().commit(
                paused,
                (EventDraft(kind=RunEventKind.RUN_PAUSED, run_id=snapshot.run_id),),
                expected_version=current.version,
            )
        return await super().commit(snapshot, events, expected_version=expected_version)


class VersionBumpBeforePauseStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.bumped = False

    async def commit(
        self,
        snapshot: RunSnapshot,
        events: tuple[EventDraft, ...],
        *,
        expected_version: int,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        is_pause = any(event.kind == RunEventKind.RUN_PAUSED for event in events)
        if is_pause and not self.bumped:
            self.bumped = True
            current = await self.load(snapshot.run_id)
            bumped = replace(current, version=current.version + 1)
            await super().commit(
                bumped,
                (EventDraft(kind="run.concurrent_update", run_id=snapshot.run_id),),
                expected_version=current.version,
            )
        return await super().commit(snapshot, events, expected_version=expected_version)


def test_runtime_executes_dependencies_in_order() -> None:
    async def scenario() -> None:
        librarian = FakeCapability("librarian")
        coder = FakeCapability("coder")
        reporter = FakeCapability("reporter")
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=FixedResearchPlanner(),
            capabilities=CapabilityRegistry((librarian, coder, reporter)),
        )

        result = await runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))

        assert result.snapshot.status == RunStatus.SUCCEEDED
        assert all(
            status == TaskStatus.SUCCEEDED for status in result.snapshot.task_statuses.values()
        )
        dependency_outputs = coder.invocations[0].inputs["dependency_outputs"]
        assert dependency_outputs == {
            "collect_sources": {
                "capability": "librarian",
                "task_id": "collect_sources",
                "summary": "librarian completed collect_sources",
            }
        }
        assert result.emitted_events[0].kind == RunEventKind.RUN_CREATED
        assert result.emitted_events[-1].kind == RunEventKind.RUN_SUCCEEDED
        assert [event.sequence for event in result.emitted_events] == list(
            range(1, len(result.emitted_events) + 1)
        )

    asyncio.run(scenario())


def test_runtime_records_planning_failure() -> None:
    async def scenario() -> None:
        capability = FakeCapability("unused")
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=FailingPlanner(),
            capabilities=CapabilityRegistry((capability,)),
        )

        result = await runtime.dispatch(StartRun(run_id="run-planning-failure", goal="Research"))

        assert result.snapshot.status == RunStatus.FAILED
        assert result.snapshot.plan is None
        assert capability.invocations == []
        assert [event.kind for event in result.emitted_events] == [
            RunEventKind.RUN_CREATED,
            RunEventKind.RUN_FAILED,
        ]
        assert result.emitted_events[-1].payload == {
            "reason": "planning_failed",
            "error_type": "RuntimeError",
            "message": "planner unavailable",
        }
        assert await runtime.get(result.snapshot.run_id) == result.snapshot

    asyncio.run(scenario())


def test_runtime_records_failure_and_blocks_downstream_tasks() -> None:
    async def scenario() -> None:
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=FixedResearchPlanner(),
            capabilities=CapabilityRegistry(
                (
                    FakeCapability("librarian"),
                    FakeCapability("coder", failure_message="experiment failed"),
                    FakeCapability("reporter"),
                )
            ),
        )

        result = await runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))

        assert result.snapshot.status == RunStatus.FAILED
        assert result.snapshot.task_statuses == {
            "collect_sources": TaskStatus.SUCCEEDED,
            "prepare_experiment": TaskStatus.FAILED,
            "write_report": TaskStatus.BLOCKED,
        }
        assert result.snapshot.task_errors == {"prepare_experiment": "experiment failed"}
        assert [event.kind for event in result.emitted_events[-2:]] == [
            RunEventKind.TASK_FAILED,
            RunEventKind.RUN_FAILED,
        ]

    asyncio.run(scenario())


def test_runtime_only_blocks_descendants_of_the_failed_task() -> None:
    class BranchedPlanner:
        async def build(self, context: PlanningContext) -> PlanDefinition:
            return PlanDefinition(
                plan_id=f"{context.run_id}-plan",
                tasks=(
                    TaskSpec(task_id="root", title="Root", capability="broken"),
                    TaskSpec(
                        task_id="dependent",
                        title="Dependent",
                        capability="unused",
                        dependencies=("root",),
                    ),
                    TaskSpec(task_id="independent", title="Independent", capability="unused"),
                ),
            )

    async def scenario() -> None:
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=BranchedPlanner(),
            capabilities=CapabilityRegistry(
                (
                    FakeCapability("broken", failure_message="root failed"),
                    FakeCapability("unused"),
                )
            ),
        )

        result = await runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))

        assert result.snapshot.task_statuses == {
            "root": TaskStatus.FAILED,
            "dependent": TaskStatus.BLOCKED,
            "independent": TaskStatus.CANCELLED,
        }

    asyncio.run(scenario())


def test_runtime_enforces_task_timeout() -> None:
    async def scenario() -> None:
        capability = BlockingCapability("controlled")
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=SingleTaskPlanner(capability.name, timeout_seconds=0.01),
            capabilities=CapabilityRegistry((capability,)),
        )

        result = await asyncio.wait_for(
            runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper")),
            timeout=1,
        )

        assert result.snapshot.status == RunStatus.FAILED
        assert "timed out after" in result.snapshot.task_errors["controlled_task"]
        failed = next(
            event for event in result.emitted_events if event.kind == RunEventKind.TASK_FAILED
        )
        assert failed.payload["error_type"] == "TimeoutError"

    asyncio.run(scenario())


def test_runtime_enforces_run_wall_clock_budget() -> None:
    async def scenario() -> None:
        capability = BlockingCapability("controlled")
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=SingleTaskPlanner(capability.name, timeout_seconds=10),
            capabilities=CapabilityRegistry((capability,)),
        )

        result = await asyncio.wait_for(
            runtime.dispatch(
                StartRun(
                    run_id="run-1",
                    goal="Reproduce a paper",
                    budget=RunBudget(max_wall_seconds=0.01),
                )
            ),
            timeout=1,
        )

        assert result.snapshot.status == RunStatus.FAILED
        assert "wall-clock budget" in result.snapshot.task_errors["controlled_task"]

    asyncio.run(scenario())


def test_result_conflict_is_reloaded_and_recorded_as_ignored() -> None:
    async def scenario() -> None:
        store = PauseBeforeResultStore()
        runtime = RuntimeService(
            store=store,
            planner=SingleTaskPlanner("controlled"),
            capabilities=CapabilityRegistry((FakeCapability("controlled"),)),
        )

        result = await runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))

        assert store.injected
        assert result.snapshot.status == RunStatus.PAUSED
        assert result.snapshot.task_statuses["controlled_task"] == TaskStatus.READY
        assert any(
            event.kind == RunEventKind.TASK_RESULT_IGNORED for event in result.emitted_events
        )

    asyncio.run(scenario())


def test_failure_conflict_is_reloaded_and_recorded_as_ignored() -> None:
    async def scenario() -> None:
        store = PauseBeforeResultStore()
        runtime = RuntimeService(
            store=store,
            planner=SingleTaskPlanner("controlled"),
            capabilities=CapabilityRegistry(
                (FakeCapability("controlled", failure_message="late failure"),)
            ),
        )

        result = await runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))

        assert store.injected
        assert result.snapshot.status == RunStatus.PAUSED
        assert result.snapshot.task_errors == {}
        assert any(
            event.kind == RunEventKind.TASK_RESULT_IGNORED for event in result.emitted_events
        )

    asyncio.run(scenario())


def test_pause_invalidates_late_result_and_resume_reexecutes_task() -> None:
    async def scenario() -> None:
        capability = BlockingCapability("controlled")
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=SingleTaskPlanner(capability.name),
            capabilities=CapabilityRegistry((capability,)),
        )
        running = asyncio.create_task(
            runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))
        )
        await capability.started.wait()

        paused = await runtime.dispatch(PauseRun(run_id="run-1"))
        capability.release.set()
        stopped = await running

        assert paused.snapshot.status == RunStatus.PAUSED
        assert stopped.snapshot.status == RunStatus.PAUSED
        assert stopped.snapshot.task_statuses["controlled_task"] == TaskStatus.READY
        assert any(
            event.kind == RunEventKind.TASK_RESULT_IGNORED for event in stopped.emitted_events
        )

        resumed = await runtime.dispatch(ResumeRun(run_id="run-1"))
        assert resumed.snapshot.status == RunStatus.SUCCEEDED
        assert capability.invocation_count == 2
        assert resumed.snapshot.task_outputs["controlled_task"] == {"invocation": 2}

    asyncio.run(scenario())


def test_pause_reloads_and_retries_after_a_version_conflict() -> None:
    async def scenario() -> None:
        capability = BlockingCapability("controlled")
        store = VersionBumpBeforePauseStore()
        runtime = RuntimeService(
            store=store,
            planner=SingleTaskPlanner(capability.name),
            capabilities=CapabilityRegistry((capability,)),
        )
        running = asyncio.create_task(
            runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))
        )
        await capability.started.wait()

        paused = await runtime.dispatch(PauseRun(run_id="run-1"))
        capability.release.set()
        await running

        assert store.bumped
        assert paused.snapshot.status == RunStatus.PAUSED
        assert [event.kind for event in await store.list_events("run-1")].count(
            RunEventKind.RUN_PAUSED
        ) == 1

    asyncio.run(scenario())


def test_cancel_invalidates_in_flight_result() -> None:
    async def scenario() -> None:
        capability = BlockingCapability("controlled")
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=SingleTaskPlanner(capability.name),
            capabilities=CapabilityRegistry((capability,)),
        )
        running = asyncio.create_task(
            runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))
        )
        await capability.started.wait()

        cancelled = await runtime.dispatch(CancelRun(run_id="run-1"))
        capability.release.set()
        stopped = await running

        assert cancelled.snapshot.status == RunStatus.CANCELLED
        assert stopped.snapshot.status == RunStatus.CANCELLED
        assert stopped.snapshot.task_statuses["controlled_task"] == TaskStatus.CANCELLED
        assert stopped.snapshot.task_outputs == {}
        assert any(
            event.kind == RunEventKind.TASK_RESULT_IGNORED for event in stopped.emitted_events
        )

    asyncio.run(scenario())


def test_unexpected_capability_error_is_recorded_as_run_evidence() -> None:
    class BrokenCapability(FakeCapability):
        async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
            raise RuntimeError("unexpected crash")

    async def scenario() -> None:
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=SingleTaskPlanner("broken"),
            capabilities=CapabilityRegistry((BrokenCapability("broken"),)),
        )

        result = await runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))

        assert result.snapshot.status == RunStatus.FAILED
        assert result.snapshot.task_errors == {"controlled_task": "unexpected crash"}
        failed_event = next(
            event for event in result.emitted_events if event.kind == RunEventKind.TASK_FAILED
        )
        assert failed_event.payload["error_type"] == "RuntimeError"

    asyncio.run(scenario())


def test_runtime_retries_and_clears_recovered_error() -> None:
    class FlakyCapability(FakeCapability):
        def __init__(self) -> None:
            super().__init__("flaky")
            self.attempts = 0

        async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
            self.attempts += 1
            if self.attempts == 1:
                raise ExecutionFailure("temporary failure")
            return CapabilityResult(outputs={"recovered": True})

    async def scenario() -> None:
        capability = FlakyCapability()
        runtime = RuntimeService(
            store=InMemoryRunStore(),
            planner=SingleTaskPlanner(capability.name, max_attempts=2),
            capabilities=CapabilityRegistry((capability,)),
        )

        result = await runtime.dispatch(StartRun(run_id="run-1", goal="Reproduce a paper"))

        assert result.snapshot.status == RunStatus.SUCCEEDED
        assert result.snapshot.task_attempts == {"controlled_task": 2}
        assert result.snapshot.task_errors == {}
        assert any(event.kind == RunEventKind.TASK_RETRYING for event in result.emitted_events)

    asyncio.run(scenario())
