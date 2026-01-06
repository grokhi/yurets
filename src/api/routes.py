from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
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

    pos = streamer.current_position_seconds()
    current = current.model_copy(update={"position_seconds": pos})
    return JSONResponse(content=current.model_dump())


@router.get("/api/master")
async def master_debug(
    request: Request,
    count: int = Query(default=10, ge=0, le=200, description="Preview tracks per slot"),
) -> JSONResponse:
    streamer = request.app.state.streamer
    settings = request.app.state.settings

    current = await streamer.now_playing.get()
    if current is None:
        current = NowPlaying(
            title="(ещё не началось)",
            source="unknown",
            duration_seconds=None,
            position_seconds=None,
            mime_type=settings.stream_mime_type,
        )
    else:
        current = current.model_copy(
            update={"position_seconds": streamer.current_position_seconds()}
        )

    preview = await streamer.preview_tracks(count_per_slot=count)
    diagnostics_stats = streamer.diagnostics()

    return JSONResponse(
        content={
            "now_playing": current.model_dump(),
            "assumed_bitrate_kbps": settings.assumed_bitrate_kbps,
            "source_chunk_size": settings.chunk_size,
            "broadcast_chunk_size": settings.broadcast_chunk_size,
            "track_buffer_chunks": settings.track_buffer_chunks,
            "subscriber_queue_chunks": settings.subscriber_queue_chunks,
            "stats": diagnostics_stats,
            "preview": preview,
        }
    )


@router.get("/api/stats")
async def stats(request: Request) -> JSONResponse:
    streamer = request.app.state.streamer
    return JSONResponse(content=streamer.diagnostics())


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
        streamer.subscribe(), media_type=settings.stream_mime_type, headers=headers
    )
