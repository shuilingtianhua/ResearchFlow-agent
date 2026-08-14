"""Composition root for production dependencies."""

from fastapi import FastAPI

from researchflow.api import create_app
from researchflow.settings import Settings


def build_application(settings: Settings | None = None) -> FastAPI:
    """Build the process boundary without hiding dependency construction elsewhere."""

    return create_app(settings or Settings.from_env())
