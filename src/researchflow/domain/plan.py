"""Validated task graph definitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from researchflow.domain.errors import ContractViolation


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    title: str
    capability: str
    dependencies: tuple[str, ...] = ()
    inputs: Mapping[str, object] = field(default_factory=dict)
    output_kinds: tuple[str, ...] = ()
    priority: int = 0
    max_attempts: int = 1
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.task_id or not self.title or not self.capability:
            raise ContractViolation("task_id, title, and capability are required")
        if self.task_id in self.dependencies:
            raise ContractViolation(f"task {self.task_id!r} cannot depend on itself")
        if self.max_attempts < 1 or self.timeout_seconds <= 0:
            raise ContractViolation("task retry and timeout values must be positive")


@dataclass(frozen=True, slots=True)
class PlanDefinition:
    plan_id: str
    tasks: tuple[TaskSpec, ...]
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.plan_id or not self.tasks:
            raise ContractViolation("plan_id and at least one task are required")
        self._validate_graph()

    def _validate_graph(self) -> None:
        task_by_id = {task.task_id: task for task in self.tasks}
        if len(task_by_id) != len(self.tasks):
            raise ContractViolation("task ids must be unique")

        for task in self.tasks:
            missing = set(task.dependencies) - task_by_id.keys()
            if missing:
                raise ContractViolation(
                    f"task {task.task_id!r} has unknown dependencies: {sorted(missing)!r}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ContractViolation("task graph must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in task_by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_by_id:
            visit(task_id)
