"""Stable public interface of the runtime module."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from researchflow.domain.artifact import ArtifactRef
from researchflow.domain.event import RunEvent
from researchflow.domain.run import RunSnapshot
from researchflow.runtime.commands import RunCommand


@dataclass(frozen=True, slots=True)
class CommandResult:
    snapshot: RunSnapshot
    emitted_events: tuple[RunEvent, ...] = ()


class ResearchRuntime(Protocol):
    async def close(self) -> None: ...

    async def recover_interrupted_runs(self) -> int: ...

    async def run_worker(self) -> None: ...

    async def submit(self, command: RunCommand) -> CommandResult: ...

    async def dispatch(self, command: RunCommand) -> CommandResult: ...

    async def get(self, run_id: str) -> RunSnapshot: ...

    async def get_artifact(self, run_id: str, artifact_id: str) -> ArtifactRef: ...

    async def open_artifact(
        self, run_id: str, artifact_id: str
    ) -> tuple[ArtifactRef, AsyncIterator[bytes]]: ...

    def events(self, run_id: str, after_sequence: int = 0) -> AsyncIterator[RunEvent]: ...

    def watch_events(self, run_id: str, after_sequence: int = 0) -> AsyncIterator[RunEvent]: ...
