"""Deterministic planner used by the first executable vertical slice."""

from __future__ import annotations

from researchflow.domain.errors import ContractViolation
from researchflow.domain.plan import PlanDefinition, TaskSpec
from researchflow.planning.contracts import PlanningContext


class FixedResearchPlanner:
    """Build a small, valid research DAG without relying on a model provider."""

    required_capabilities = ("librarian", "coder", "reporter")

    async def build(self, context: PlanningContext) -> PlanDefinition:
        missing = set(self.required_capabilities) - set(context.available_capabilities)
        if missing:
            raise ContractViolation(f"planner capabilities are unavailable: {sorted(missing)!r}")

        return PlanDefinition(
            plan_id=f"{context.run_id}-plan",
            tasks=(
                TaskSpec(
                    task_id="collect_sources",
                    title="Collect research sources",
                    capability="librarian",
                    inputs={"goal": context.goal},
                ),
                TaskSpec(
                    task_id="prepare_experiment",
                    title="Prepare experiment workspace",
                    capability="coder",
                    dependencies=("collect_sources",),
                    inputs={"goal": context.goal},
                ),
                TaskSpec(
                    task_id="write_report",
                    title="Write evidence-based report",
                    capability="reporter",
                    dependencies=("prepare_experiment",),
                    inputs={"goal": context.goal},
                ),
            ),
        )
