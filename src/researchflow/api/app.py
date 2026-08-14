"""FastAPI application factory; business services are injected here later."""

from __future__ import annotations

from fastapi import FastAPI

from researchflow.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    app = FastAPI(title=resolved.app_name, version="0.0.0")

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": resolved.app_name,
            "environment": resolved.environment,
        }

    return app
