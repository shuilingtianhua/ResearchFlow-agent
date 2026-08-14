"""Append-only run event contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunEventKind(StrEnum):
    RUN_CREATED = "run.created"
    PLAN_CREATED = "plan.created"
    RUN_STARTED = "run.started"
    RUN_RECOVERED = "run.recovered"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_CANCELLED = "run.cancelled"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    TASK_READY = "task.ready"
    TASK_STARTED = "task.started"
    TASK_SUCCEEDED = "task.succeeded"
    TASK_RETRYING = "task.retrying"
    TASK_FAILED = "task.failed"
    TASK_RESULT_IGNORED = "task.result_ignored"


@dataclass(frozen=True, slots=True)
class EventDraft:
    kind: RunEventKind | str
    run_id: str
    task_id: str | None = None
    execution_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class RunEvent:
    sequence: int
    kind: RunEventKind | str
    run_id: str
    task_id: str | None = None
    execution_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utc_now)
