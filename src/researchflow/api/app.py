"""FastAPI application factory and thin HTTP boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from researchflow.api.schemas import EventResponse, RunActionRequest, RunResponse, StartRunRequest
from researchflow.domain.errors import ConflictError, ContractViolation, NotFoundError
from researchflow.runtime import CancelRun, PauseRun, ResearchRuntime, ResumeRun, StartRun
from researchflow.settings import Settings


def create_app(runtime: ResearchRuntime, settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.recover_interrupted_runs()
        yield

    app = FastAPI(title=resolved.app_name, version="0.0.0", lifespan=lifespan)
    app.state.runtime = runtime

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, exc)

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, exc)

    @app.exception_handler(ContractViolation)
    async def invalid_contract(_: Request, exc: ContractViolation) -> JSONResponse:
        return _error_response(422, exc)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": resolved.app_name,
            "environment": resolved.environment,
        }

    @app.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
    async def start_run(request: StartRunRequest) -> RunResponse:
        result = await runtime.dispatch(
            StartRun(
                run_id=request.run_id or str(uuid4()),
                goal=request.goal,
                inputs=request.inputs,
            )
        )
        return RunResponse.from_snapshot(result.snapshot)

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        return RunResponse.from_snapshot(await runtime.get(run_id))

    @app.get("/runs/{run_id}/events", response_model=list[EventResponse])
    async def get_events(run_id: str, after_sequence: int = 0) -> list[EventResponse]:
        events = [
            EventResponse.from_event(event)
            async for event in runtime.events(run_id, after_sequence)
        ]
        return events

    @app.post("/runs/{run_id}/pause", response_model=RunResponse)
    async def pause_run(run_id: str, request: RunActionRequest) -> RunResponse:
        result = await runtime.dispatch(PauseRun(run_id=run_id, reason=request.reason))
        return RunResponse.from_snapshot(result.snapshot)

    @app.post("/runs/{run_id}/resume", response_model=RunResponse)
    async def resume_run(run_id: str) -> RunResponse:
        result = await runtime.dispatch(ResumeRun(run_id=run_id))
        return RunResponse.from_snapshot(result.snapshot)

    @app.post("/runs/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: str, request: RunActionRequest) -> RunResponse:
        result = await runtime.dispatch(CancelRun(run_id=run_id, reason=request.reason))
        return RunResponse.from_snapshot(result.snapshot)

    return app


def _error_response(status_code: int, error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": type(error).__name__, "detail": str(error)},
    )
