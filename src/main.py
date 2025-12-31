from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.api.routes import router
from src.settings import Settings
from src.streaming.streamer import Streamer


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    streamer: Streamer = fastapi_app.state.streamer

    await streamer.startup()
    try:
        yield
    finally:
        await streamer.shutdown()


def create_app() -> FastAPI:
    settings = Settings()
    app = FastAPI(title="Юрец ФМ", lifespan=lifespan)

    app.state.settings = settings
    app.state.streamer = Streamer(settings=settings)

    app.include_router(router)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    return app


application = create_app()
