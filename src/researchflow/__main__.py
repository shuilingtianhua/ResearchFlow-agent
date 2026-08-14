"""Development server entry point."""

from __future__ import annotations

import uvicorn

from researchflow.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "researchflow.bootstrap:build_application",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
