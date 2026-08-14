"""Stable public interface of the runtime module."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from researchflow.domain.event import RunEvent
from researchflow.domain.run import RunSnapshot
from researchflow.runtime.commands import RunCommand


@dataclass(frozen=True, slots=True)
class CommandResult:
    snapshot: RunSnapshot
    emitted_events: tuple[RunEvent, ...] = ()


class ResearchRuntime(Protocol):
    async def dispatch(self, command: RunCommand) -> CommandResult: ...

    async def get(self, run_id: str) -> RunSnapshot: ...

    def events(self, run_id: str, after_sequence: int = 0) -> AsyncIterator[RunEvent]: ...
