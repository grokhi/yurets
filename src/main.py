from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes import router, set_listener_cookie, visitor_context
from src.services.analytics import Analytics
from src.services.database import Database
from src.settings import Settings
from src.streaming.sources.plex import PlexError
from src.streaming.streamer import Streamer


def create_app(settings: Settings | None = None, *, source=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(Path(settings.data_dir) / "radio.sqlite3")
        analytics = Analytics(settings, database)
        streamer = Streamer(settings, database, source)
        app.state.database, app.state.analytics, app.state.streamer = database, analytics, streamer

        async def maintain():
            while True:
                await asyncio.sleep(3600)
                await asyncio.to_thread(analytics.purge)

        await streamer.startup()
        maintenance = asyncio.create_task(maintain())
        try:
            yield
        finally:
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass
            await streamer.shutdown()
            analytics.close()
            database.close()

    app = FastAPI(title="Радио Юрец", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    static_dir = Path(__file__).resolve().parent / "static"

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(("/static/", "/api/artwork/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PlexError)
    async def plex_error(request: Request, exc: PlexError):
        return JSONResponse(
            {"detail": "Plex временно недоступен", "code": str(exc)}, status_code=503
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        visitor, ip, country = visitor_context(request)
        await asyncio.to_thread(
            app.state.analytics.visit, visitor, ip, country, request.headers.get("user-agent", "")
        )
        response = FileResponse(static_dir / "index.html", media_type="text/html")
        set_listener_cookie(request, response, visitor)
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


application = create_app()
