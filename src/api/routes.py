from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from src.models.now_playing import NowPlaying

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/now-playing", response_model=NowPlaying)
async def now_playing(request: Request) -> JSONResponse:
    streamer = request.app.state.streamer
    current = await streamer.now_playing.get()

    if current is None:
        # предсказуемое дефолтное значение
        current = NowPlaying(
            title="(ещё не началось)",
            source="unknown",
            duration_seconds=None,
            mime_type=request.app.state.settings.stream_mime_type,
        )

    return JSONResponse(content=current.model_dump())


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    streamer = request.app.state.streamer
    settings = request.app.state.settings

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }

    return StreamingResponse(
        streamer.stream(), media_type=settings.stream_mime_type, headers=headers
    )
