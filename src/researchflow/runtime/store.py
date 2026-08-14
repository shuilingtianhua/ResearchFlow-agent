"""Persistence port owned by the runtime that consumes it."""

from __future__ import annotations

from typing import Protocol

from researchflow.domain.event import EventDraft, RunEvent
from researchflow.domain.run import RunSnapshot


class RunStore(Protocol):
    async def create(
        self, snapshot: RunSnapshot, events: tuple[EventDraft, ...] = ()
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...

    async def load(self, run_id: str) -> RunSnapshot: ...

    async def commit(
        self,
        snapshot: RunSnapshot,
        events: tuple[EventDraft, ...],
        *,
        expected_version: int,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...

    async def list_events(self, run_id: str, after_sequence: int = 0) -> tuple[RunEvent, ...]: ...
