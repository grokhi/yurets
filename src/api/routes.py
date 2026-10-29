from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from src.models.now_playing import NowPlaying

router = APIRouter()
basic = HTTPBasic(auto_error=False)


def require_admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
    expected = request.app.state.settings.admin_password.get_secret_value()
    if not expected:
        raise HTTPException(503, "Статистика ещё не настроена")
    if (
        credentials is None
        or credentials.username != "radio"
        or not secrets.compare_digest(credentials.password.encode(), expected.encode())
    ):
        raise HTTPException(
            401, "Требуется вход", headers={"WWW-Authenticate": 'Basic realm="Yurets statistics"'}
        )


@router.get("/health")
async def health(request: Request):
    streamer = request.app.state.streamer
    return JSONResponse(
        {
            "status": "ok" if streamer.ready else "unavailable",
            "source": "plex",
            "error": streamer.diagnostics()["error"],
        },
        status_code=200 if streamer.ready else 503,
    )


@router.get("/api/now-playing", response_model=NowPlaying)
async def now_playing(request: Request):
    streamer = request.app.state.streamer
    current = await streamer.now_playing.get() or NowPlaying(
        title="Ожидание эфира",
        source="Plex",
        mime_type="audio/mpeg",
    )
    return current.model_copy(update={"position_seconds": streamer.current_position_seconds()})


@router.get("/api/queue")
async def queue(request: Request, count: int = Query(default=5, ge=0, le=50)):
    return await request.app.state.streamer.queue_preview(count)


@router.get("/api/master")
async def master(request: Request, queue_count: int = Query(default=5, ge=0, le=50)):
    streamer = request.app.state.streamer
    return {
        "now_playing": (await now_playing(request)).model_dump(),
        "queue": await streamer.queue_preview(queue_count),
        "stats": streamer.diagnostics(),
    }


@router.get("/api/stats")
async def stats(request: Request):
    # Public technical stream counters only; visitor records are admin-only.
    return request.app.state.streamer.diagnostics()


@router.get("/api/schedule")
async def schedule(request: Request):
    streamer = request.app.state.streamer
    return await streamer.program.schedule(streamer.now())


@router.get("/stream")
async def stream(request: Request):
    streamer = request.app.state.streamer
    if not streamer.ready:
        raise HTTPException(503, "Эфир временно недоступен", headers={"Retry-After": "5"})
    return StreamingResponse(
        streamer.subscribe(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def visitor_context(request: Request):
    analytics = request.app.state.analytics
    visitor = analytics.identify(request.cookies.get(analytics.cookie_name))
    ip, country = analytics.location(
        request.client.host if request.client else "unknown",
        request.headers.get("x-forwarded-for", ""),
    )
    return visitor, ip, country


def set_listener_cookie(request: Request, response: Response, visitor: str):
    analytics = request.app.state.analytics
    response.set_cookie(
        analytics.cookie_name,
        analytics.signed(visitor),
        max_age=365 * 86400,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.secure_cookies,
    )


class ListeningEvent(BaseModel):
    action: Literal["start", "heartbeat", "stop"]
    session_id: str | None = Field(default=None, max_length=64)
    position: float = Field(ge=0, le=1_000_000_000, allow_inf_nan=False)


@router.post("/api/listening")
async def listening(request: Request, event: ListeningEvent):
    visitor, ip, country = visitor_context(request)
    session_id = await asyncio.to_thread(
        request.app.state.analytics.listening,
        visitor,
        event.action,
        event.session_id,
        event.position,
        ip,
        country,
    )
    response = JSONResponse({"session_id": session_id})
    set_listener_cookie(request, response, visitor)
    return response


@router.get("/api/analytics", dependencies=[Depends(require_admin)])
async def analytics(request: Request, days: int = Query(default=30, ge=1, le=365)):
    return await asyncio.to_thread(request.app.state.analytics.summary, days)


@router.get("/stats", include_in_schema=False, dependencies=[Depends(require_admin)])
async def statistics_page():
    return FileResponse(
        Path(__file__).resolve().parents[1] / "pages" / "stats.html", media_type="text/html"
    )
