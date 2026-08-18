"""Versioned HTTP request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field

from researchflow.domain.artifact import ArtifactRef
from researchflow.domain.event import RunEvent, RunEventKind
from researchflow.domain.run import RunSnapshot


class StartRunRequest(BaseModel):
    run_id: str | None = Field(default=None, min_length=1)
    goal: str = Field(min_length=1)
    inputs: dict[str, object] = Field(default_factory=dict)


class RunActionRequest(BaseModel):
    reason: str = Field(default="user_requested", min_length=1)


class ArtifactResponse(BaseModel):
    artifact_id: str
    kind: str
    uri: str
    sha256: str
    producer_task_id: str | None
    schema_version: str
    metadata: dict[str, object]

    @classmethod
    def from_ref(cls, artifact: ArtifactRef) -> Self:
        return cls(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            uri=artifact.uri,
            sha256=artifact.sha256,
            producer_task_id=artifact.producer_task_id,
            schema_version=artifact.schema_version,
            metadata=dict(artifact.metadata),
        )


class RunResponse(BaseModel):
    run_id: str
    goal: str
    status: str
    version: int
    plan_id: str | None
    task_statuses: dict[str, str]
    task_outputs: dict[str, dict[str, object]]
    task_artifacts: dict[str, tuple[ArtifactRef, ...]]
    task_errors: dict[str, str]
    task_attempts: dict[str, int]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_snapshot(cls, snapshot: RunSnapshot) -> Self:
        return cls(
            run_id=snapshot.run_id,
            goal=snapshot.goal,
            status=snapshot.status.value,
            version=snapshot.version,
            plan_id=snapshot.plan.plan_id if snapshot.plan else None,
            task_statuses={
                task_id: status.value for task_id, status in snapshot.task_statuses.items()
            },
            task_outputs={
                task_id: dict(outputs) for task_id, outputs in snapshot.task_outputs.items()
            },
            task_artifacts={
                task_id: tuple(artifacts) for task_id, artifacts in snapshot.task_artifacts.items()
            },
            task_errors=dict(snapshot.task_errors),
            task_attempts=dict(snapshot.task_attempts),
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
        )


class EventResponse(BaseModel):
    sequence: int
    kind: str
    run_id: str
    task_id: str | None
    execution_id: str | None
    payload: dict[str, object]
    occurred_at: datetime

    @classmethod
    def from_event(cls, event: RunEvent) -> Self:
        kind = event.kind.value if isinstance(event.kind, RunEventKind) else event.kind
        return cls(
            sequence=event.sequence,
            kind=kind,
            run_id=event.run_id,
            task_id=event.task_id,
            execution_id=event.execution_id,
            payload=dict(event.payload),
            occurred_at=event.occurred_at,
        )
