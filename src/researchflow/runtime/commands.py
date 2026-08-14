"""Commands accepted by the research runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

from researchflow.domain.errors import ContractViolation
from researchflow.domain.run import RunBudget


@dataclass(frozen=True, slots=True)
class StartRun:
    run_id: str
    goal: str
    inputs: Mapping[str, object] = field(default_factory=dict)
    budget: RunBudget = field(default_factory=RunBudget)

    def __post_init__(self) -> None:
        if not self.run_id or not self.goal.strip():
            raise ContractViolation("run_id and goal are required")


@dataclass(frozen=True, slots=True)
class PauseRun:
    run_id: str
    reason: str = "user_requested"


@dataclass(frozen=True, slots=True)
class ResumeRun:
    run_id: str


@dataclass(frozen=True, slots=True)
class CancelRun:
    run_id: str
    reason: str = "user_requested"


RunCommand: TypeAlias = StartRun | PauseRun | ResumeRun | CancelRun
