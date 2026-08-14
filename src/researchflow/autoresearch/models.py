"""Versioned contracts shared by both AutoResearch modes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from researchflow.domain.artifact import ArtifactRef


class AutoResearchMode(StrEnum):
    CODE_PATCH = "code_patch"
    CONFIGURATION = "configuration"


class TrialDecision(StrEnum):
    KEEP = "keep"
    REJECT = "reject"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class FrozenResearchContract:
    objective: str
    metric_name: str
    metric_direction: str
    evaluator_digest: str
    dataset_digest: str
    max_trials: int
    repository_revision: str | None = None
    schema_version: str = "1"
    constraints: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    description: str
    payload: Mapping[str, object]
    parent_candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrialRecord:
    trial_id: str
    candidate: Candidate
    decision: TrialDecision
    metric: float | None
    evidence: tuple[ArtifactRef, ...] = ()
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AutoResearchRequest:
    run_id: str
    mode: AutoResearchMode
    contract: FrozenResearchContract


@dataclass(frozen=True, slots=True)
class AutoResearchResult:
    best_candidate: Candidate | None
    trials: tuple[TrialRecord, ...]
    final_metric: float | None
    validation_evidence: tuple[ArtifactRef, ...] = ()
