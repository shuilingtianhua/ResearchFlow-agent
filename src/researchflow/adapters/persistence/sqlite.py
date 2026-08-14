"""SQLite implementation of the runtime-owned RunStore port."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypeVar

from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from researchflow.domain.errors import (
    ConflictError,
    ContractViolation,
    DependencyUnavailable,
    NotFoundError,
)
from researchflow.domain.event import EventDraft, RunEvent
from researchflow.domain.run import RunSnapshot, RunStatus

_metadata = MetaData()
_runs = Table(
    "runs",
    _metadata,
    Column("run_id", String, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("snapshot_json", Text, nullable=False),
)
_run_events = Table(
    "run_events",
    _metadata,
    Column("run_id", String, ForeignKey("runs.run_id"), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("event_json", Text, nullable=False),
)

_snapshot_adapter = TypeAdapter(RunSnapshot)
_event_adapter = TypeAdapter(RunEvent)
_T = TypeVar("_T")


class SQLiteRunStore:
    """Persist immutable run snapshots and append-only events in SQLite."""

    def __init__(self, database_url: str) -> None:
        try:
            url = make_url(database_url)
        except ArgumentError as exc:
            raise ContractViolation("database_url must be a valid SQLAlchemy URL") from exc
        if url.drivername != "sqlite+aiosqlite":
            raise ContractViolation("SQLiteRunStore requires a sqlite+aiosqlite database URL")
        self._database_path = _database_path(url.database)
        # NullPool avoids sharing async driver connections across TestClient event loops.
        pool = StaticPool if self._database_path is None else NullPool
        self._engine: AsyncEngine = create_async_engine(database_url, poolclass=pool)
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def create(
        self, snapshot: RunSnapshot, events: tuple[EventDraft, ...] = ()
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        if snapshot.version != 0:
            raise ContractViolation("a new run must start at version 0")
        _validate_event_run_ids(snapshot.run_id, events)
        snapshot_json = _serialize_snapshot(snapshot)
        await self._ensure_initialized()

        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    insert(_runs).values(
                        run_id=snapshot.run_id,
                        version=snapshot.version,
                        snapshot_json=snapshot_json,
                    )
                )
                stored_events = await self._append_events(connection, snapshot.run_id, events)
        except IntegrityError as exc:
            raise ConflictError(f"run {snapshot.run_id!r} already exists") from exc
        except DBAPIError as exc:
            raise _database_error(exc) from exc
        return snapshot, stored_events

    async def load(self, run_id: str) -> RunSnapshot:
        await self._ensure_initialized()
        try:
            async with self._engine.connect() as connection:
                snapshot_json = await connection.scalar(
                    select(_runs.c.snapshot_json).where(_runs.c.run_id == run_id)
                )
        except DBAPIError as exc:
            raise _database_error(exc) from exc
        if snapshot_json is None:
            raise NotFoundError(f"run {run_id!r} was not found")
        return _deserialize_snapshot(snapshot_json)

    async def list_by_status(self, statuses: frozenset[RunStatus]) -> tuple[RunSnapshot, ...]:
        await self._ensure_initialized()
        try:
            async with self._engine.connect() as connection:
                rows = await connection.execute(
                    select(_runs.c.snapshot_json).order_by(_runs.c.run_id)
                )
        except DBAPIError as exc:
            raise _database_error(exc) from exc
        snapshots = (_deserialize_snapshot(value) for value in rows.scalars())
        return tuple(snapshot for snapshot in snapshots if snapshot.status in statuses)

    async def commit(
        self,
        snapshot: RunSnapshot,
        events: tuple[EventDraft, ...],
        *,
        expected_version: int,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        if snapshot.version != expected_version + 1:
            raise ContractViolation("committed snapshot version must advance exactly once")
        _validate_event_run_ids(snapshot.run_id, events)
        snapshot_json = _serialize_snapshot(snapshot)
        await self._ensure_initialized()

        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(
                    update(_runs)
                    .where(
                        _runs.c.run_id == snapshot.run_id,
                        _runs.c.version == expected_version,
                    )
                    .values(version=snapshot.version, snapshot_json=snapshot_json)
                )
                if result.rowcount != 1:
                    await self._raise_commit_conflict(connection, snapshot.run_id, expected_version)
                stored_events = await self._append_events(connection, snapshot.run_id, events)
        except DBAPIError as exc:
            raise _database_error(exc) from exc
        return snapshot, stored_events

    async def list_events(self, run_id: str, after_sequence: int = 0) -> tuple[RunEvent, ...]:
        await self._ensure_initialized()
        try:
            async with self._engine.connect() as connection:
                exists = await connection.scalar(
                    select(_runs.c.run_id).where(_runs.c.run_id == run_id)
                )
                if exists is None:
                    raise NotFoundError(f"run {run_id!r} was not found")
                rows = await connection.execute(
                    select(_run_events.c.event_json)
                    .where(
                        _run_events.c.run_id == run_id,
                        _run_events.c.sequence > after_sequence,
                    )
                    .order_by(_run_events.c.sequence)
                )
        except DBAPIError as exc:
            raise _database_error(exc) from exc
        return tuple(_deserialize_event(value) for value in rows.scalars())

    async def close(self) -> None:
        await self._engine.dispose()

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            try:
                if self._database_path is not None:
                    self._database_path.parent.mkdir(parents=True, exist_ok=True)
                async with self._engine.begin() as connection:
                    await connection.run_sync(_metadata.create_all)
            except OSError as exc:
                raise DependencyUnavailable(
                    f"SQLite persistence directory is unavailable: {exc}"
                ) from exc
            except DBAPIError as exc:
                raise _database_error(exc) from exc
            self._initialized = True

    async def _append_events(
        self,
        connection: AsyncConnection,
        run_id: str,
        drafts: tuple[EventDraft, ...],
    ) -> tuple[RunEvent, ...]:
        if not drafts:
            return ()
        last_sequence = await connection.scalar(
            select(func.coalesce(func.max(_run_events.c.sequence), 0)).where(
                _run_events.c.run_id == run_id
            )
        )
        stored = _materialize_events(run_id, int(last_sequence or 0), drafts)
        rows = [
            {
                "run_id": event.run_id,
                "sequence": event.sequence,
                "event_json": _serialize_event(event),
            }
            for event in stored
        ]
        await connection.execute(insert(_run_events), rows)
        return stored

    async def _raise_commit_conflict(
        self, connection: AsyncConnection, run_id: str, expected_version: int
    ) -> None:
        current_version = await connection.scalar(
            select(_runs.c.version).where(_runs.c.run_id == run_id)
        )
        if current_version is None:
            raise NotFoundError(f"run {run_id!r} was not found")
        raise ConflictError(
            f"run version changed: expected {expected_version}, found {current_version}"
        )


def _database_path(database: str | None) -> Path | None:
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser()


def _validate_event_run_ids(run_id: str, drafts: tuple[EventDraft, ...]) -> None:
    if any(draft.run_id != run_id for draft in drafts):
        raise ContractViolation("event run_id must match the snapshot run_id")


def _materialize_events(
    run_id: str, last_sequence: int, drafts: tuple[EventDraft, ...]
) -> tuple[RunEvent, ...]:
    return tuple(
        RunEvent(
            sequence=last_sequence + offset,
            kind=draft.kind,
            run_id=run_id,
            task_id=draft.task_id,
            execution_id=draft.execution_id,
            payload=dict(draft.payload),
            occurred_at=draft.occurred_at,
        )
        for offset, draft in enumerate(drafts, start=1)
    )


def _serialize_snapshot(snapshot: RunSnapshot) -> str:
    return _serialize(_snapshot_adapter, snapshot, "run snapshot")


def _serialize_event(event: RunEvent) -> str:
    return _serialize(_event_adapter, event, "run event")


def _serialize(adapter: TypeAdapter[_T], value: _T, label: str) -> str:
    try:
        return adapter.dump_json(value).decode("utf-8")
    except (PydanticSerializationError, TypeError, ValueError) as exc:
        raise ContractViolation(f"{label} must contain JSON-serializable values") from exc


def _deserialize_snapshot(value: str) -> RunSnapshot:
    return _deserialize(_snapshot_adapter, value, "run snapshot")


def _deserialize_event(value: str) -> RunEvent:
    return _deserialize(_event_adapter, value, "run event")


def _deserialize(adapter: TypeAdapter[_T], value: str, label: str) -> _T:
    try:
        return adapter.validate_json(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DependencyUnavailable(f"stored {label} is invalid") from exc


def _database_error(error: DBAPIError) -> DependencyUnavailable:
    return DependencyUnavailable(f"SQLite persistence is unavailable: {error.orig}")
