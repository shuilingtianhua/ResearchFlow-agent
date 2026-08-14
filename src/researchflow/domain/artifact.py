"""Artifact references and provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from researchflow.domain.errors import ContractViolation


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    uri: str
    sha256: str
    producer_task_id: str | None = None
    schema_version: str = "1"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.kind or not self.uri:
            raise ContractViolation("artifact_id, kind, and uri are required")
        if len(self.sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.sha256):
            raise ContractViolation("sha256 must be a lowercase 64-character digest")
