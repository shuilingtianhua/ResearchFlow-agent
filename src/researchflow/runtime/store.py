"""Persistence port owned by the runtime that consumes it."""

from __future__ import annotations

from typing import Protocol

from researchflow.domain.event import EventDraft, RunEvent
from researchflow.domain.run import RunSnapshot, RunStatus


class RunStore(Protocol):
    async def close(self) -> None: ...

    async def create(
        self, snapshot: RunSnapshot, events: tuple[EventDraft, ...] = ()
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...

    async def load(self, run_id: str) -> RunSnapshot: ...

    async def list_by_status(self, statuses: frozenset[RunStatus]) -> tuple[RunSnapshot, ...]: ...

    async def commit(
        self,
        snapshot: RunSnapshot,
        events: tuple[EventDraft, ...],
        *,
        expected_version: int,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...

    async def list_events(self, run_id: str, after_sequence: int = 0) -> tuple[RunEvent, ...]: ...
