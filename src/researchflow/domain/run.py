"""Research run snapshots and budgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping

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


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    goal: str
    status: RunStatus = RunStatus.CREATED
    version: int = 0
    plan: PlanDefinition | None = None
    task_statuses: Mapping[str, TaskStatus] = field(default_factory=dict)
    budget: RunBudget = field(default_factory=RunBudget)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
