import pytest

from researchflow.domain.errors import ContractViolation
from researchflow.domain.plan import PlanDefinition, TaskSpec


def test_plan_accepts_a_valid_dag() -> None:
    plan = PlanDefinition(
        plan_id="plan-1",
        tasks=(
            TaskSpec(task_id="read", title="Read paper", capability="librarian"),
            TaskSpec(
                task_id="run",
                title="Run experiment",
                capability="research_coding",
                dependencies=("read",),
            ),
        ),
    )

    assert plan.tasks[1].dependencies == ("read",)


def test_plan_rejects_a_cycle() -> None:
    with pytest.raises(ContractViolation, match="acyclic"):
        PlanDefinition(
            plan_id="plan-1",
            tasks=(
                TaskSpec(task_id="a", title="A", capability="coder", dependencies=("b",)),
                TaskSpec(task_id="b", title="B", capability="coder", dependencies=("a",)),
            ),
        )
