"""Narrow interface implemented by every research capability."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from researchflow.domain.artifact import ArtifactRef
from researchflow.domain.errors import ContractViolation


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    run_id: str
    task_id: str
    execution_id: str
    inputs: Mapping[str, object] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    outputs: Mapping[str, object] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()


class Capability(Protocol):
    @property
    def name(self) -> str: ...

    async def invoke(self, request: CapabilityRequest) -> CapabilityResult: ...


class CapabilityRegistry:
    def __init__(self, capabilities: tuple[Capability, ...] = ()) -> None:
        self._capabilities: dict[str, Capability] = {}
        for capability in capabilities:
            self.register(capability)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._capabilities))

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ContractViolation(f"capability {capability.name!r} is already registered")
        self._capabilities[capability.name] = capability

    async def invoke(self, name: str, request: CapabilityRequest) -> CapabilityResult:
        try:
            capability = self._capabilities[name]
        except KeyError as exc:
            raise ContractViolation(f"unknown capability: {name!r}") from exc
        return await capability.invoke(request)
