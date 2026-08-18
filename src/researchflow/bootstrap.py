"""Composition root for production dependencies."""

from fastapi import FastAPI

from researchflow.adapters.artifacts import FilesystemArtifactStore
from researchflow.adapters.capabilities import FakeCapability
from researchflow.adapters.persistence import SQLiteRunStore
from researchflow.api import create_app
from researchflow.capabilities import CapabilityRegistry
from researchflow.planning import FixedResearchPlanner
from researchflow.runtime import ResearchRuntime, RuntimeService
from researchflow.settings import Settings


def build_runtime(settings: Settings) -> ResearchRuntime:
    capabilities = CapabilityRegistry(
        (
            FakeCapability("librarian"),
            FakeCapability("coder"),
            FakeCapability("reporter"),
        )
    )
    return RuntimeService(
        store=SQLiteRunStore(settings.database_url),
        planner=FixedResearchPlanner(),
        capabilities=capabilities,
        artifact_store=FilesystemArtifactStore(settings.artifact_root),
    )


def build_application(settings: Settings | None = None) -> FastAPI:
    """Build the process boundary without hiding dependency construction elsewhere."""

    resolved = settings or Settings.from_env()
    return create_app(runtime=build_runtime(resolved), settings=resolved)
