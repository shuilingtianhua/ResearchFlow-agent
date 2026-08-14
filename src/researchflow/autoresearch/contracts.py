"""Scientific AutoResearch application boundary."""

from __future__ import annotations

from typing import Protocol

from researchflow.autoresearch.models import AutoResearchRequest, AutoResearchResult


class AutoResearchModule(Protocol):
    async def run(self, request: AutoResearchRequest) -> AutoResearchResult: ...
