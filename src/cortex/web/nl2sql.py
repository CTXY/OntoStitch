"""HTTP snapshots, commands and replayable SSE for interactive NL2SQL."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cortex.application.online_context import OnlineContextError
from cortex.capabilities.nl2sql.service import public_task


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OntologySource(Input):
    kind: Literal["archive", "run", "import"]
    id: str = Field(default="", max_length=100)


class SessionRequest(Input):
    database_id: str = Field(min_length=1, max_length=100)
    ontology_source: OntologySource
    request_id: str = Field(min_length=1, max_length=100)


class TaskRequest(Input):
    question: str = Field(min_length=1, max_length=12000)
    request_id: str = Field(min_length=1, max_length=100)
    revision_of: str | None = None


class Answer(Input):
    question_id: str
    selected_option_ids: list[str] = Field(default_factory=list, max_length=100)
    explanation: str = Field(default="", max_length=12000)


class AnswersRequest(Input):
    interaction_id: str
    expected_state_version: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=100)
    answers: list[Answer] = Field(min_length=1, max_length=50)


def query_router(service, sources):
    router = APIRouter(prefix="/api/nl2sql", tags=["NL2SQL"])

    def get(key, kind):
        try:
            return service.store.get(key, kind)
        except KeyError:
            raise HTTPException(404, f"Unknown {kind}") from None

    @router.get("/sources")
    def graph_sources(database_id: str):
        service.database_path(database_id)
        return {"sources": sources(database_id)}

    @router.post("/sessions", status_code=201)
    async def create_session(body: SessionRequest):
        try:
            return await service.create_session(
                body.database_id, body.ontology_source.model_dump(), body.request_id
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)[:1200]) from exc

    @router.get("/sessions")
    def sessions(database_id: str | None = None):
        return {
            "sessions": [
                service.public_session(s)
                for s in service.store.list("session")
                if not s.get("archived_at")
                and (database_id is None or s["database_id"] == database_id)
            ]
        }

    @router.post("/sessions/{key}/archive")
    async def archive_session(key: str):
        get(key, "session")
        try:
            return await service.archive_session(key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/sessions/{key}")
    def session(key: str):
        return service.public_session(get(key, "session"))

    @router.post("/sessions/{key}/tasks", status_code=202)
    async def create_task(key: str, body: TaskRequest):
        get(key, "session")
        try:
            return await service.create_task(
                key, body.question, body.request_id, body.revision_of
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/tasks/{key}")
    def task(key: str):
        return public_task(get(key, "task"))

    @router.get("/tasks/{key}/activity")
    def activity(key: str, after: int = Query(0, ge=0)):
        get(key, "task")
        return {"events": service.store.events(key, after)}

    @router.get("/tasks/{key}/events")
    async def events(key: str, request: Request, after: int = Query(0, ge=0)):
        get(key, "task")
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "Invalid event cursor") from None

        async def stream():
            nonlocal cursor
            ticks = 0
            while not await request.is_disconnected():
                entries = service.store.events(key, cursor)
                for entry in entries:
                    cursor = entry["seq"]
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"
                task = service.store.get(key, "task")
                if (
                    task["status"] not in {"queued", "running"}
                    and cursor >= task["last_event_seq"]
                ):
                    yield f"event: settled\ndata: {json.dumps({'status': task['status']})}\n\n"
                    return
                ticks += 1
                if ticks % 20 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/tasks/{key}/answers", status_code=202)
    async def answers(key: str, body: AnswersRequest):
        get(key, "task")
        try:
            return await service.answer(key, body.model_dump())
        except (ValidationError, OnlineContextError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            message = str(exc)
            code = (
                422
                if any(
                    t in message
                    for t in (
                        "answers must cover",
                        "unknown option",
                        "requires an option",
                        "unique",
                    )
                )
                else 409
            )
            raise HTTPException(code, message) from exc

    @router.post("/tasks/{key}/retry", status_code=202)
    async def retry(key: str):
        get(key, "task")
        try:
            return await service.retry(key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/tasks/{key}/cancel")
    async def cancel(key: str):
        get(key, "task")
        return await service.cancel(key)

    @router.get("/tasks/{key}/artifacts/{artifact}")
    def artifact(key: str, artifact: str):
        task = get(key, "task")
        if artifact in task["graphs"]:
            return {"graph": task["graphs"][artifact]}
        if artifact == "result":
            return task["result"]
        for revision in public_task(task)["sql_revisions"]:
            if revision["id"] == artifact:
                return revision
        raise HTTPException(404, "Unknown task artifact")

    return router
