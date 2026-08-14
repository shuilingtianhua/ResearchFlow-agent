"""Deterministic in-memory RunStore for tests and early vertical slices."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from researchflow.domain.errors import ConflictError, ContractViolation, NotFoundError
from researchflow.domain.event import EventDraft, RunEvent
from researchflow.domain.run import RunSnapshot, RunStatus


class InMemoryRunStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, RunSnapshot] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, snapshot: RunSnapshot, events: tuple[EventDraft, ...] = ()
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        async with self._lock:
            if snapshot.run_id in self._snapshots:
                raise ConflictError(f"run {snapshot.run_id!r} already exists")
            if snapshot.version != 0:
                raise ContractViolation("a new run must start at version 0")
            stored_events = self._append(snapshot.run_id, events)
            self._snapshots[snapshot.run_id] = deepcopy(snapshot)
            return deepcopy(snapshot), deepcopy(stored_events)

    async def load(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            try:
                return deepcopy(self._snapshots[run_id])
            except KeyError as exc:
                raise NotFoundError(f"run {run_id!r} was not found") from exc

    async def list_by_status(self, statuses: frozenset[RunStatus]) -> tuple[RunSnapshot, ...]:
        async with self._lock:
            return tuple(
                deepcopy(snapshot)
                for run_id, snapshot in sorted(self._snapshots.items())
                if snapshot.status in statuses
            )

    async def commit(
        self,
        snapshot: RunSnapshot,
        events: tuple[EventDraft, ...],
        *,
        expected_version: int,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        async with self._lock:
            try:
                current = self._snapshots[snapshot.run_id]
            except KeyError as exc:
                raise NotFoundError(f"run {snapshot.run_id!r} was not found") from exc
            if current.version != expected_version:
                raise ConflictError(
                    f"run version changed: expected {expected_version}, found {current.version}"
                )
            if snapshot.version != expected_version + 1:
                raise ContractViolation("committed snapshot version must advance exactly once")
            stored_events = self._append(snapshot.run_id, events)
            self._snapshots[snapshot.run_id] = deepcopy(snapshot)
            return deepcopy(snapshot), deepcopy(stored_events)

    async def list_events(self, run_id: str, after_sequence: int = 0) -> tuple[RunEvent, ...]:
        async with self._lock:
            if run_id not in self._snapshots:
                raise NotFoundError(f"run {run_id!r} was not found")
            return tuple(
                deepcopy(event) for event in self._events[run_id] if event.sequence > after_sequence
            )

    def _append(self, run_id: str, drafts: tuple[EventDraft, ...]) -> tuple[RunEvent, ...]:
        stream = self._events.setdefault(run_id, [])
        if any(draft.run_id != run_id for draft in drafts):
            raise ContractViolation("event run_id must match the snapshot run_id")

        stored: list[RunEvent] = []
        for offset, draft in enumerate(drafts, start=1):
            event = RunEvent(
                sequence=len(stream) + offset,
                kind=draft.kind,
                run_id=draft.run_id,
                task_id=draft.task_id,
                execution_id=draft.execution_id,
                payload=deepcopy(draft.payload),
                occurred_at=draft.occurred_at,
            )
            stored.append(event)
        stream.extend(stored)
        return tuple(stored)
