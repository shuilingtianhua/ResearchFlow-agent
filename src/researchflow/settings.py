"""Application configuration loaded at the composition boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Small, explicit configuration surface for the first vertical slice."""

    app_name: str = "ResearchFlow Agent"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = "sqlite+aiosqlite:///./data/researchflow.db"
    artifact_root: str = "./artifacts"

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        return cls(
            app_name=os.getenv("RESEARCHFLOW_APP_NAME", defaults.app_name),
            environment=os.getenv("RESEARCHFLOW_ENV", defaults.environment),
            host=os.getenv("RESEARCHFLOW_HOST", defaults.host),
            port=int(os.getenv("RESEARCHFLOW_PORT", str(defaults.port))),
            database_url=os.getenv("RESEARCHFLOW_DATABASE_URL", defaults.database_url),
            artifact_root=os.getenv("RESEARCHFLOW_ARTIFACT_ROOT", defaults.artifact_root),
        )
