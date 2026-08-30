"""Minimal FastAPI adapter for local single-user testing."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from wiki_agent.application import (
    InvalidInputError,
    SessionNotFoundError,
    WikiAgentService,
)
from wiki_agent.application.runtime import AppRuntime


class CreateSessionRequest(BaseModel):
    title: str = Field(default="未命名", max_length=200)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


def create_app(
    *,
    project_root: Path | None = None,
    runtime: AppRuntime | None = None,
) -> FastAPI:
    """Create the local Web application.

    ``runtime`` is injectable for tests.  Production callers normally pass
    ``project_root`` and let the factory construct one process-wide runtime.
    """
    app_runtime = runtime or AppRuntime.from_project_root(project_root or Path.cwd())
    service = WikiAgentService(app_runtime)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with app_runtime:
            yield

    app = FastAPI(title="wiki-agent", version="0.1.0", lifespan=lifespan)
    app.state.runtime = app_runtime
    app.state.service = service

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return [asdict(session) for session in service.list_sessions()]

    @app.get("/api/wiki/files")
    async def list_wiki_files() -> list[dict[str, Any]]:
        return [asdict(file) for file in service.list_wiki_files()]

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        try:
            return asdict(service.create_session(title=request.title))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            return asdict(service.get_session(session_id))
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/messages")
    async def get_session_messages(session_id: str) -> list[dict[str, str]]:
        try:
            return [asdict(message) for message in service.get_session_messages(session_id)]
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: MessageRequest) -> dict[str, Any]:
        try:
            result = await service.send_message(session_id, request.text)
            return asdict(result)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/messages/stream")
    async def stream_message(session_id: str, request: MessageRequest) -> StreamingResponse:
        try:
            service.get_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def events() -> AsyncIterator[str]:
            try:
                async for event in service.stream_message(session_id, request.text):
                    payload = json.dumps(asdict(event), ensure_ascii=False)
                    yield f"event: {event.type}\ndata: {payload}\n\n"
            except (SessionNotFoundError, InvalidInputError) as exc:
                payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    frontend_dir = (project_root or Path.cwd()) / "frontend"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


app = create_app()
