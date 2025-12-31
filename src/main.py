from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
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

    index_path = static_dir / "index.html"
    favicon_svg_path = static_dir / "favicon.svg"

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        }
        return FileResponse(path=str(index_path), media_type="text/html", headers=headers)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico() -> FileResponse:
        # Часть браузеров всегда запрашивает /favicon.ico.
        # Отдаём наш SVG (современные браузеры понимают), чтобы была иконка во вкладке.
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        }
        return FileResponse(path=str(favicon_svg_path), media_type="image/svg+xml", headers=headers)

    app.mount("/static", StaticFiles(directory=str(static_dir), html=False), name="static")
    return app


application = create_app()
