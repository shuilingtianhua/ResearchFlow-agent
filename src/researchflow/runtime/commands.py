"""Commands accepted by the research runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, TypeAlias

from researchflow.domain.run import RunBudget


@dataclass(frozen=True, slots=True)
class StartRun:
    run_id: str
    goal: str
    inputs: Mapping[str, object] = field(default_factory=dict)
    budget: RunBudget = field(default_factory=RunBudget)


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
