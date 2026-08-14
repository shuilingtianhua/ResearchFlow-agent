"""Application service implementing the first complete research run lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import NoReturn
from uuid import uuid4

from researchflow.capabilities import CapabilityRegistry, CapabilityRequest, CapabilityResult
from researchflow.domain.errors import ContractViolation
from researchflow.domain.event import EventDraft, RunEvent, RunEventKind, utc_now
from researchflow.domain.plan import TaskSpec, TaskStatus
from researchflow.domain.run import RunSnapshot, RunStatus
from researchflow.planning import PlanningContext, PlanningModule
from researchflow.runtime.commands import CancelRun, PauseRun, ResumeRun, RunCommand, StartRun
from researchflow.runtime.contracts import CommandResult
from researchflow.runtime.store import RunStore


class RuntimeService:
    def __init__(
        self,
        store: RunStore,
        planner: PlanningModule,
        capabilities: CapabilityRegistry,
    ) -> None:
        self._store = store
        self._planner = planner
        self._capabilities = capabilities

    async def dispatch(self, command: RunCommand) -> CommandResult:
        if isinstance(command, StartRun):
            return await self._start(command)
        if isinstance(command, PauseRun):
            return await self._pause(command)
        if isinstance(command, ResumeRun):
            return await self._resume(command)
        if isinstance(command, CancelRun):
            return await self._cancel(command)
        return self._unsupported_command(command)

    async def get(self, run_id: str) -> RunSnapshot:
        return await self._store.load(run_id)

    async def events(self, run_id: str, after_sequence: int = 0) -> AsyncIterator[RunEvent]:
        for event in await self._store.list_events(run_id, after_sequence):
            yield event

    async def _start(self, command: StartRun) -> CommandResult:
        created = RunSnapshot(run_id=command.run_id, goal=command.goal, budget=command.budget)
        await self._store.create(
            created,
            (EventDraft(kind=RunEventKind.RUN_CREATED, run_id=command.run_id),),
        )
        planned = await self._plan(created, command)
        running = await self._change_run_status(
            planned,
            RunStatus.RUNNING,
            RunEventKind.RUN_STARTED,
        )
        final = await self._execute_until_stopped(running.run_id)
        events = await self._store.list_events(running.run_id)
        return CommandResult(snapshot=final, emitted_events=events)

    async def _plan(self, snapshot: RunSnapshot, command: StartRun) -> RunSnapshot:
        plan = await self._planner.build(
            PlanningContext(
                run_id=snapshot.run_id,
                goal=snapshot.goal,
                available_capabilities=self._capabilities.names,
                inputs=command.inputs,
                budget=snapshot.budget,
            )
        )
        statuses = {task.task_id: TaskStatus.PENDING for task in plan.tasks}
        planned = replace(snapshot, status=RunStatus.PLANNED, plan=plan, task_statuses=statuses)
        event = EventDraft(
            kind=RunEventKind.PLAN_CREATED,
            run_id=snapshot.run_id,
            payload={"plan_id": plan.plan_id, "task_count": len(plan.tasks)},
        )
        return await self._commit(snapshot, planned, (event,))

    async def _pause(self, command: PauseRun) -> CommandResult:
        snapshot = await self._store.load(command.run_id)
        if snapshot.status not in {RunStatus.PLANNED, RunStatus.RUNNING}:
            raise ContractViolation(f"cannot pause a run in {snapshot.status.value!r}")

        statuses = dict(snapshot.task_statuses)
        execution_ids = dict(snapshot.task_execution_ids)
        for task_id, status in statuses.items():
            if status == TaskStatus.RUNNING:
                statuses[task_id] = TaskStatus.READY
                execution_ids.pop(task_id, None)
        paused = replace(
            snapshot,
            status=RunStatus.PAUSED,
            task_statuses=statuses,
            task_execution_ids=execution_ids,
        )
        event = EventDraft(
            kind=RunEventKind.RUN_PAUSED,
            run_id=snapshot.run_id,
            payload={"reason": command.reason},
        )
        stored, events = await self._commit_with_events(snapshot, paused, (event,))
        return CommandResult(stored, events)

    async def _resume(self, command: ResumeRun) -> CommandResult:
        snapshot = await self._store.load(command.run_id)
        if snapshot.status != RunStatus.PAUSED:
            raise ContractViolation(f"cannot resume a run in {snapshot.status.value!r}")
        previous_events = await self._store.list_events(snapshot.run_id)
        after_sequence = previous_events[-1].sequence if previous_events else 0
        running = await self._change_run_status(
            snapshot,
            RunStatus.RUNNING,
            RunEventKind.RUN_RESUMED,
        )
        final = await self._execute_until_stopped(running.run_id)
        events = await self._store.list_events(running.run_id, after_sequence)
        return CommandResult(final, events)

    async def _cancel(self, command: CancelRun) -> CommandResult:
        snapshot = await self._store.load(command.run_id)
        if snapshot.status == RunStatus.CANCELLED:
            return CommandResult(snapshot)
        if snapshot.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            raise ContractViolation(f"cannot cancel a run in {snapshot.status.value!r}")

        statuses = {
            task_id: status if status == TaskStatus.SUCCEEDED else TaskStatus.CANCELLED
            for task_id, status in snapshot.task_statuses.items()
        }
        cancelled = replace(
            snapshot,
            status=RunStatus.CANCELLED,
            task_statuses=statuses,
            task_execution_ids={},
        )
        event = EventDraft(
            kind=RunEventKind.RUN_CANCELLED,
            run_id=snapshot.run_id,
            payload={"reason": command.reason},
        )
        stored, events = await self._commit_with_events(snapshot, cancelled, (event,))
        return CommandResult(stored, events)

    async def _execute_until_stopped(self, run_id: str) -> RunSnapshot:
        while True:
            snapshot = await self._store.load(run_id)
            if snapshot.status != RunStatus.RUNNING:
                return snapshot
            if self._all_tasks_succeeded(snapshot):
                return await self._complete(snapshot)

            task = self._next_runnable_task(snapshot)
            if task is None:
                return await self._fail_blocked_run(snapshot)
            if snapshot.task_statuses[task.task_id] == TaskStatus.PENDING:
                snapshot = await self._mark_ready(snapshot, task)
            await self._execute_task(snapshot, task)

    def _next_runnable_task(self, snapshot: RunSnapshot) -> TaskSpec | None:
        if snapshot.plan is None:
            raise ContractViolation("a running run must have a plan")
        for task in snapshot.plan.tasks:
            status = snapshot.task_statuses[task.task_id]
            if status == TaskStatus.READY:
                return task
            dependencies_succeeded = all(
                snapshot.task_statuses[dependency] == TaskStatus.SUCCEEDED
                for dependency in task.dependencies
            )
            if status == TaskStatus.PENDING and dependencies_succeeded:
                return task
        return None

    async def _mark_ready(self, snapshot: RunSnapshot, task: TaskSpec) -> RunSnapshot:
        statuses = dict(snapshot.task_statuses)
        statuses[task.task_id] = TaskStatus.READY
        ready = replace(snapshot, task_statuses=statuses)
        event = EventDraft(
            kind=RunEventKind.TASK_READY,
            run_id=snapshot.run_id,
            task_id=task.task_id,
        )
        return await self._commit(snapshot, ready, (event,))

    async def _execute_task(self, snapshot: RunSnapshot, task: TaskSpec) -> None:
        execution_id = str(uuid4())
        running = self._task_running(snapshot, task, execution_id)
        attempt = running.task_attempts[task.task_id]
        event = EventDraft(
            kind=RunEventKind.TASK_STARTED,
            run_id=snapshot.run_id,
            task_id=task.task_id,
            execution_id=execution_id,
            payload={"attempt": attempt, "capability": task.capability},
        )
        running = await self._commit(snapshot, running, (event,))

        try:
            result = await self._capabilities.invoke(
                task.capability,
                self._capability_request(running, task, execution_id),
            )
        # Capability implementations are an extension boundary; crashes become run evidence.
        except Exception as exc:  # noqa: BLE001
            await self._record_task_failure(running.run_id, task, execution_id, exc)
            return
        await self._record_task_success(running.run_id, task, execution_id, result)

    def _task_running(
        self, snapshot: RunSnapshot, task: TaskSpec, execution_id: str
    ) -> RunSnapshot:
        statuses = dict(snapshot.task_statuses)
        statuses[task.task_id] = TaskStatus.RUNNING
        attempts = dict(snapshot.task_attempts)
        attempts[task.task_id] = attempts.get(task.task_id, 0) + 1
        execution_ids = dict(snapshot.task_execution_ids)
        execution_ids[task.task_id] = execution_id
        return replace(
            snapshot,
            task_statuses=statuses,
            task_attempts=attempts,
            task_execution_ids=execution_ids,
        )

    def _capability_request(
        self, snapshot: RunSnapshot, task: TaskSpec, execution_id: str
    ) -> CapabilityRequest:
        inputs = dict(task.inputs)
        inputs["dependency_outputs"] = {
            dependency: snapshot.task_outputs.get(dependency, {})
            for dependency in task.dependencies
        }
        return CapabilityRequest(
            run_id=snapshot.run_id,
            task_id=task.task_id,
            execution_id=execution_id,
            inputs=inputs,
        )

    async def _record_task_success(
        self,
        run_id: str,
        task: TaskSpec,
        execution_id: str,
        result: CapabilityResult,
    ) -> None:
        snapshot = await self._store.load(run_id)
        if not self._is_active_execution(snapshot, task.task_id, execution_id):
            await self._record_ignored_result(snapshot, task.task_id, execution_id)
            return

        statuses = dict(snapshot.task_statuses)
        statuses[task.task_id] = TaskStatus.SUCCEEDED
        outputs = dict(snapshot.task_outputs)
        outputs[task.task_id] = dict(result.outputs)
        errors = dict(snapshot.task_errors)
        errors.pop(task.task_id, None)
        execution_ids = dict(snapshot.task_execution_ids)
        execution_ids.pop(task.task_id, None)
        succeeded = replace(
            snapshot,
            task_statuses=statuses,
            task_outputs=outputs,
            task_errors=errors,
            task_execution_ids=execution_ids,
        )
        event = EventDraft(
            kind=RunEventKind.TASK_SUCCEEDED,
            run_id=run_id,
            task_id=task.task_id,
            execution_id=execution_id,
            payload={"output_keys": sorted(result.outputs)},
        )
        await self._commit(snapshot, succeeded, (event,))

    async def _record_task_failure(
        self,
        run_id: str,
        task: TaskSpec,
        execution_id: str,
        error: Exception,
    ) -> None:
        snapshot = await self._store.load(run_id)
        if not self._is_active_execution(snapshot, task.task_id, execution_id):
            await self._record_ignored_result(snapshot, task.task_id, execution_id)
            return

        message = str(error) or type(error).__name__
        if snapshot.task_attempts[task.task_id] < task.max_attempts:
            await self._retry_task(snapshot, task, execution_id, message)
            return
        await self._fail_task_and_run(snapshot, task, execution_id, error, message)

    async def _retry_task(
        self, snapshot: RunSnapshot, task: TaskSpec, execution_id: str, message: str
    ) -> None:
        statuses = dict(snapshot.task_statuses)
        statuses[task.task_id] = TaskStatus.READY
        errors = dict(snapshot.task_errors)
        errors[task.task_id] = message
        execution_ids = dict(snapshot.task_execution_ids)
        execution_ids.pop(task.task_id, None)
        retrying = replace(
            snapshot,
            task_statuses=statuses,
            task_errors=errors,
            task_execution_ids=execution_ids,
        )
        event = EventDraft(
            kind=RunEventKind.TASK_RETRYING,
            run_id=snapshot.run_id,
            task_id=task.task_id,
            execution_id=execution_id,
            payload={"reason": message},
        )
        await self._commit(snapshot, retrying, (event,))

    async def _fail_task_and_run(
        self,
        snapshot: RunSnapshot,
        task: TaskSpec,
        execution_id: str,
        error: Exception,
        message: str,
    ) -> None:
        statuses = {
            task_id: self._terminal_task_status(task_id, status, task.task_id)
            for task_id, status in snapshot.task_statuses.items()
        }
        errors = dict(snapshot.task_errors)
        errors[task.task_id] = message
        failed = replace(
            snapshot,
            status=RunStatus.FAILED,
            task_statuses=statuses,
            task_errors=errors,
            task_execution_ids={},
        )
        events = (
            EventDraft(
                kind=RunEventKind.TASK_FAILED,
                run_id=snapshot.run_id,
                task_id=task.task_id,
                execution_id=execution_id,
                payload={"error_type": type(error).__name__, "message": message},
            ),
            EventDraft(
                kind=RunEventKind.RUN_FAILED,
                run_id=snapshot.run_id,
                payload={"failed_task_id": task.task_id},
            ),
        )
        await self._commit(snapshot, failed, events)

    def _terminal_task_status(
        self, task_id: str, status: TaskStatus, failed_task_id: str
    ) -> TaskStatus:
        if task_id == failed_task_id:
            return TaskStatus.FAILED
        if status == TaskStatus.SUCCEEDED:
            return status
        return TaskStatus.CANCELLED

    def _is_active_execution(self, snapshot: RunSnapshot, task_id: str, execution_id: str) -> bool:
        return (
            snapshot.status == RunStatus.RUNNING
            and snapshot.task_statuses.get(task_id) == TaskStatus.RUNNING
            and snapshot.task_execution_ids.get(task_id) == execution_id
        )

    async def _record_ignored_result(
        self, snapshot: RunSnapshot, task_id: str, execution_id: str
    ) -> None:
        event = EventDraft(
            kind=RunEventKind.TASK_RESULT_IGNORED,
            run_id=snapshot.run_id,
            task_id=task_id,
            execution_id=execution_id,
            payload={"reason": "execution_is_no_longer_active"},
        )
        await self._commit(snapshot, snapshot, (event,))

    async def _complete(self, snapshot: RunSnapshot) -> RunSnapshot:
        return await self._change_run_status(
            snapshot,
            RunStatus.SUCCEEDED,
            RunEventKind.RUN_SUCCEEDED,
        )

    async def _fail_blocked_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        statuses = {
            task_id: status if status == TaskStatus.SUCCEEDED else TaskStatus.CANCELLED
            for task_id, status in snapshot.task_statuses.items()
        }
        failed = replace(snapshot, status=RunStatus.FAILED, task_statuses=statuses)
        event = EventDraft(
            kind=RunEventKind.RUN_FAILED,
            run_id=snapshot.run_id,
            payload={"reason": "no_runnable_tasks"},
        )
        return await self._commit(snapshot, failed, (event,))

    async def _change_run_status(
        self,
        snapshot: RunSnapshot,
        status: RunStatus,
        event_kind: RunEventKind,
    ) -> RunSnapshot:
        changed = replace(snapshot, status=status)
        event = EventDraft(kind=event_kind, run_id=snapshot.run_id)
        return await self._commit(snapshot, changed, (event,))

    async def _commit(
        self,
        previous: RunSnapshot,
        changed: RunSnapshot,
        events: tuple[EventDraft, ...],
    ) -> RunSnapshot:
        stored, _ = await self._commit_with_events(previous, changed, events)
        return stored

    async def _commit_with_events(
        self,
        previous: RunSnapshot,
        changed: RunSnapshot,
        events: tuple[EventDraft, ...],
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        versioned = replace(changed, version=previous.version + 1, updated_at=utc_now())
        return await self._store.commit(
            versioned,
            events,
            expected_version=previous.version,
        )

    def _all_tasks_succeeded(self, snapshot: RunSnapshot) -> bool:
        return bool(snapshot.task_statuses) and all(
            status == TaskStatus.SUCCEEDED for status in snapshot.task_statuses.values()
        )

    def _unsupported_command(self, command: object) -> NoReturn:
        raise ContractViolation(f"unsupported command: {type(command).__name__}")
