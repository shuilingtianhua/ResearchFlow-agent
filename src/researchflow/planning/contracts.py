"""Plan construction boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from researchflow.domain.plan import PlanDefinition
from researchflow.domain.run import RunBudget


@dataclass(frozen=True, slots=True)
class PlanningContext:
    run_id: str
    goal: str
    available_capabilities: tuple[str, ...]
    inputs: Mapping[str, object] = field(default_factory=dict)
    budget: RunBudget = field(default_factory=RunBudget)


class PlanningModule(Protocol):
    async def build(self, context: PlanningContext) -> PlanDefinition: ...
