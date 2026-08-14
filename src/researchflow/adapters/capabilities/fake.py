"""Deterministic capability implementation for the M1 execution loop."""

from __future__ import annotations

from researchflow.capabilities import CapabilityRequest, CapabilityResult
from researchflow.domain.errors import ExecutionFailure


class FakeCapability:
    def __init__(self, name: str, *, failure_message: str | None = None) -> None:
        self._name = name
        self._failure_message = failure_message
        self.invocations: list[CapabilityRequest] = []

    @property
    def name(self) -> str:
        return self._name

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        self.invocations.append(request)
        if self._failure_message is not None:
            raise ExecutionFailure(self._failure_message)
        return CapabilityResult(
            outputs={
                "capability": self.name,
                "task_id": request.task_id,
                "summary": f"{self.name} completed {request.task_id}",
            }
        )
