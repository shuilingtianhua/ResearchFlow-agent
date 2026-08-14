"""Composition root for production dependencies."""

from fastapi import FastAPI

from researchflow.adapters.capabilities import FakeCapability
from researchflow.adapters.persistence import InMemoryRunStore
from researchflow.api import create_app
from researchflow.capabilities import CapabilityRegistry
from researchflow.planning import FixedResearchPlanner
from researchflow.runtime import ResearchRuntime, RuntimeService
from researchflow.settings import Settings


def build_runtime() -> ResearchRuntime:
    capabilities = CapabilityRegistry(
        (
            FakeCapability("librarian"),
            FakeCapability("coder"),
            FakeCapability("reporter"),
        )
    )
    return RuntimeService(
        store=InMemoryRunStore(),
        planner=FixedResearchPlanner(),
        capabilities=capabilities,
    )


def build_application(settings: Settings | None = None) -> FastAPI:
    """Build the process boundary without hiding dependency construction elsewhere."""

    resolved = settings or Settings.from_env()
    return create_app(runtime=build_runtime(), settings=resolved)
