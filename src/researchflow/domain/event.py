"""Append-only run event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class EventDraft:
    kind: str
    run_id: str
    task_id: str | None = None
    execution_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class RunEvent:
    sequence: int
    kind: str
    run_id: str
    task_id: str | None = None
    execution_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utc_now)
