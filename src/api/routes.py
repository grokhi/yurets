from __future__ import annotations

from pathlib import Path

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


@router.get("/api/schedule")
async def schedule(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    streamer = request.app.state.streamer

    slots: list[dict[str, object]] = []
    for slot in settings.schedule():
        label = slot.source
        if slot.source == "telegram":
            if slot.key and streamer.telegram_session.enabled():
                label = await streamer.telegram_session.display_name(slot.key)
            elif slot.key:
                label = slot.key
            else:
                label = "telegram"
        elif slot.source == "local":
            label = Path(slot.key).name if slot.key else "local"

        slots.append(
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "source": label,
                "key": slot.key,
            }
        )

    return JSONResponse(content={"slots": slots})


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
