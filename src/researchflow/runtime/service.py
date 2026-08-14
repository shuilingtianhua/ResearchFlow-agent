"""Application service implementing the first complete research run lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import NoReturn
from uuid import uuid4

from anyio import fail_after

from researchflow.capabilities import CapabilityRegistry, CapabilityRequest, CapabilityResult
from researchflow.domain.errors import ConflictError, ContractViolation
from researchflow.domain.event import EventDraft, RunEvent, RunEventKind, utc_now
from researchflow.domain.plan import TaskSpec, TaskStatus
from researchflow.domain.run import RunSnapshot, RunStatus
from researchflow.planning import PlanningContext, PlanningModule
from researchflow.runtime.commands import CancelRun, PauseRun, ResumeRun, RunCommand, StartRun
from researchflow.runtime.contracts import CommandResult
from researchflow.runtime.store import RunStore

_COMMIT_ATTEMPTS = 5


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

    async def recover_interrupted_runs(self) -> int:
        snapshots = await self._store.list_by_status(frozenset({RunStatus.RUNNING}))
        recovered_count = 0
        for snapshot in snapshots:
            if await self._recover_interrupted_run(snapshot.run_id):
                recovered_count += 1
        return recovered_count

    async def _recover_interrupted_run(self, run_id: str) -> bool:
        for _ in range(_COMMIT_ATTEMPTS):
            snapshot = await self._store.load(run_id)
            if snapshot.status != RunStatus.RUNNING:
                return False

            paused, _ = self._paused_transition(snapshot, "process_restart")
            event = EventDraft(
                kind=RunEventKind.RUN_RECOVERED,
                run_id=snapshot.run_id,
                payload={
                    "previous_status": snapshot.status.value,
                    "reason": "process_restart",
                    "invalidated_execution_count": len(snapshot.task_execution_ids),
                },
            )
            try:
                await self._commit(snapshot, paused, (event,))
                return True
            except ConflictError:
                continue
        raise ConflictError(f"recovery for {run_id!r} kept conflicting")

    async def _start(self, command: StartRun) -> CommandResult:
        created = RunSnapshot(run_id=command.run_id, goal=command.goal, budget=command.budget)
        await self._store.create(
            created,
            (EventDraft(kind=RunEventKind.RUN_CREATED, run_id=command.run_id),),
        )
        planned = await self._plan(created, command)
        if planned.status == RunStatus.FAILED:
            events = await self._store.list_events(planned.run_id)
            return CommandResult(snapshot=planned, emitted_events=events)
        running = await self._change_run_status(
            planned,
            RunStatus.RUNNING,
            RunEventKind.RUN_STARTED,
        )
        final = await self._execute_until_stopped(running.run_id)
        events = await self._store.list_events(running.run_id)
        return CommandResult(snapshot=final, emitted_events=events)

    async def _plan(self, snapshot: RunSnapshot, command: StartRun) -> RunSnapshot:
        try:
            plan = await self._planner.build(
                PlanningContext(
                    run_id=snapshot.run_id,
                    goal=snapshot.goal,
                    available_capabilities=self._capabilities.names,
                    inputs=command.inputs,
                    budget=snapshot.budget,
                )
            )
        except Exception as error:  # noqa: BLE001 - planner is an untrusted plugin boundary
            return await self._fail_planning(snapshot, error)

        statuses = {task.task_id: TaskStatus.PENDING for task in plan.tasks}
        planned = replace(snapshot, status=RunStatus.PLANNED, plan=plan, task_statuses=statuses)
        event = EventDraft(
            kind=RunEventKind.PLAN_CREATED,
            run_id=snapshot.run_id,
            payload={"plan_id": plan.plan_id, "task_count": len(plan.tasks)},
        )
        return await self._commit(snapshot, planned, (event,))

    async def _fail_planning(self, snapshot: RunSnapshot, error: Exception) -> RunSnapshot:
        failed = replace(snapshot, status=RunStatus.FAILED)
        event = EventDraft(
            kind=RunEventKind.RUN_FAILED,
            run_id=snapshot.run_id,
            payload={
                "reason": "planning_failed",
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )
        return await self._commit(snapshot, failed, (event,))

    async def _pause(self, command: PauseRun) -> CommandResult:
        for _ in range(_COMMIT_ATTEMPTS):
            snapshot = await self._store.load(command.run_id)
            if snapshot.status == RunStatus.PAUSED:
                return CommandResult(snapshot)
            if snapshot.status not in {RunStatus.PLANNED, RunStatus.RUNNING}:
                raise ContractViolation(f"cannot pause a run in {snapshot.status.value!r}")

            paused, event = self._paused_transition(snapshot, command.reason)
            try:
                stored, events = await self._commit_with_events(snapshot, paused, (event,))
                return CommandResult(stored, events)
            except ConflictError:
                continue
        raise ConflictError(f"pause command for {command.run_id!r} kept conflicting")

    def _paused_transition(
        self, snapshot: RunSnapshot, reason: str
    ) -> tuple[RunSnapshot, EventDraft]:
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
            payload={"reason": reason},
        )
        return paused, event

    async def _resume(self, command: ResumeRun) -> CommandResult:
        for _ in range(_COMMIT_ATTEMPTS):
            snapshot = await self._store.load(command.run_id)
            if snapshot.status != RunStatus.PAUSED:
                raise ContractViolation(f"cannot resume a run in {snapshot.status.value!r}")
            previous_events = await self._store.list_events(snapshot.run_id)
            after_sequence = previous_events[-1].sequence if previous_events else 0
            try:
                running = await self._change_run_status(
                    snapshot,
                    RunStatus.RUNNING,
                    RunEventKind.RUN_RESUMED,
                )
            except ConflictError:
                continue
            final = await self._execute_until_stopped(running.run_id)
            events = await self._store.list_events(running.run_id, after_sequence)
            return CommandResult(final, events)
        raise ConflictError(f"resume command for {command.run_id!r} kept conflicting")

    async def _cancel(self, command: CancelRun) -> CommandResult:
        for _ in range(_COMMIT_ATTEMPTS):
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
            try:
                stored, events = await self._commit_with_events(snapshot, cancelled, (event,))
                return CommandResult(stored, events)
            except ConflictError:
                continue
        raise ConflictError(f"cancel command for {command.run_id!r} kept conflicting")

    async def _execute_until_stopped(self, run_id: str) -> RunSnapshot:
        while True:
            snapshot = await self._store.load(run_id)
            if snapshot.status != RunStatus.RUNNING:
                return snapshot
            if self._all_tasks_succeeded(snapshot):
                try:
                    return await self._complete(snapshot)
                except ConflictError:
                    continue

            task = self._next_runnable_task(snapshot)
            if task is None:
                try:
                    return await self._fail_blocked_run(snapshot)
                except ConflictError:
                    continue
            if snapshot.task_statuses[task.task_id] == TaskStatus.PENDING:
                try:
                    snapshot = await self._mark_ready(snapshot, task)
                except ConflictError:
                    continue
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
        try:
            running = await self._commit(snapshot, running, (event,))
        except ConflictError:
            return
        timeout_seconds, timeout_message = self._execution_timeout(running, task)

        try:
            if timeout_seconds <= 0:
                raise TimeoutError(timeout_message)
            with fail_after(timeout_seconds):
                result = await self._capabilities.invoke(
                    task.capability,
                    self._capability_request(running, task, execution_id),
                )
        except TimeoutError:
            await self._record_task_failure(
                running.run_id,
                task,
                execution_id,
                TimeoutError(timeout_message),
            )
            return
        # Capability implementations are an extension boundary; crashes become run evidence.
        except Exception as exc:  # noqa: BLE001
            await self._record_task_failure(running.run_id, task, execution_id, exc)
            return
        await self._record_task_success(running.run_id, task, execution_id, result)

    def _execution_timeout(self, snapshot: RunSnapshot, task: TaskSpec) -> tuple[float, str]:
        elapsed = max(0.0, (utc_now() - snapshot.created_at).total_seconds())
        remaining_budget = snapshot.budget.max_wall_seconds - elapsed
        if remaining_budget <= task.timeout_seconds:
            return (
                max(0.0, remaining_budget),
                f"run wall-clock budget of {snapshot.budget.max_wall_seconds:g} seconds exhausted",
            )
        return task.timeout_seconds, f"task timed out after {task.timeout_seconds:g} seconds"

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
        for _ in range(_COMMIT_ATTEMPTS):
            snapshot = await self._store.load(run_id)
            try:
                if not self._is_active_execution(snapshot, task.task_id, execution_id):
                    await self._record_ignored_result(snapshot, task.task_id, execution_id)
                    return
                await self._commit_task_success(snapshot, task, execution_id, result)
                return
            except ConflictError:
                continue
        raise ConflictError(f"task result for {task.task_id!r} kept conflicting")

    async def _commit_task_success(
        self,
        snapshot: RunSnapshot,
        task: TaskSpec,
        execution_id: str,
        result: CapabilityResult,
    ) -> None:
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
            run_id=snapshot.run_id,
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
        for _ in range(_COMMIT_ATTEMPTS):
            snapshot = await self._store.load(run_id)
            try:
                if not self._is_active_execution(snapshot, task.task_id, execution_id):
                    await self._record_ignored_result(snapshot, task.task_id, execution_id)
                    return

                message = str(error) or type(error).__name__
                if snapshot.task_attempts[task.task_id] < task.max_attempts:
                    await self._retry_task(snapshot, task, execution_id, message)
                    return
                await self._fail_task_and_run(snapshot, task, execution_id, error, message)
                return
            except ConflictError:
                continue
        raise ConflictError(f"task failure for {task.task_id!r} kept conflicting")

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
        blocked_task_ids = self._blocked_descendants(snapshot, task.task_id)
        statuses = {
            task_id: self._terminal_task_status(
                task_id,
                status,
                task.task_id,
                blocked_task_ids,
            )
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
        self,
        task_id: str,
        status: TaskStatus,
        failed_task_id: str,
        blocked_task_ids: set[str],
    ) -> TaskStatus:
        if task_id == failed_task_id:
            return TaskStatus.FAILED
        if status == TaskStatus.SUCCEEDED:
            return status
        if task_id in blocked_task_ids:
            return TaskStatus.BLOCKED
        return TaskStatus.CANCELLED

    def _blocked_descendants(self, snapshot: RunSnapshot, failed_task_id: str) -> set[str]:
        if snapshot.plan is None:
            return set()
        blocked = {failed_task_id}
        while True:
            expanded = blocked | {
                task.task_id
                for task in snapshot.plan.tasks
                if any(dependency in blocked for dependency in task.dependencies)
            }
            if expanded == blocked:
                return blocked - {failed_task_id}
            blocked = expanded

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
            task_id: status if status == TaskStatus.SUCCEEDED else TaskStatus.BLOCKED
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
