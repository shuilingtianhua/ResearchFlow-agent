"""Research run snapshots and budgets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from researchflow.domain.errors import ContractViolation
from researchflow.domain.event import utc_now
from researchflow.domain.plan import PlanDefinition, TaskStatus


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_wall_seconds: float = 3600.0
    max_model_tokens: int = 200_000
    max_experiments: int = 50
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        if self.max_wall_seconds <= 0:
            raise ContractViolation("max_wall_seconds must be positive")
        if self.max_model_tokens < 0 or self.max_experiments < 0:
            raise ContractViolation("model token and experiment budgets cannot be negative")
        if self.max_concurrency < 1:
            raise ContractViolation("max_concurrency must be at least one")


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    goal: str
    status: RunStatus = RunStatus.CREATED
    version: int = 0
    plan: PlanDefinition | None = None
    task_statuses: Mapping[str, TaskStatus] = field(default_factory=dict)
    task_outputs: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    task_errors: Mapping[str, str] = field(default_factory=dict)
    task_attempts: Mapping[str, int] = field(default_factory=dict)
    task_execution_ids: Mapping[str, str] = field(default_factory=dict)
    budget: RunBudget = field(default_factory=RunBudget)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.run_id or not self.goal.strip():
            raise ContractViolation("run_id and goal are required")
