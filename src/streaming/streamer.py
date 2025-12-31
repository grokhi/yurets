from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from src.models.now_playing import NowPlaying
from src.services.now_playing import NowPlayingState
from src.services.scheduler import Scheduler
from src.settings import Settings
from src.streaming.sources.local import LocalLibrarySource
from src.streaming.sources.telegram import TelegramChannelSource


class Streamer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.now_playing = NowPlayingState()
        self.scheduler = Scheduler(slots=settings.schedule())

        self.local_source = LocalLibrarySource(music_dir=settings.local_music_dir)
        self.telegram_source = TelegramChannelSource(settings=settings.telegram())

    async def startup(self) -> None:
        if self.telegram_source.enabled():
            await self.telegram_source.startup()

    async def shutdown(self) -> None:
        await self.telegram_source.shutdown()

    def _choose_source(self) -> str:
        return self.scheduler.choose_source(datetime.now())

    def _get_source(self, source_id: str):
        if source_id == self.telegram_source.id and self.telegram_source.enabled():
            return self.telegram_source
        return self.local_source

    async def stream(self) -> AsyncIterator[bytes]:
        # Непрерывный поток: по треку за раз, бесконечно.
        while True:
            source_id = self._choose_source()
            source = self._get_source(source_id)

            track = await source.next_track(mime_type=self._settings.stream_mime_type)

            await self.now_playing.set(
                NowPlaying(
                    title=track.title,
                    source=source.id,
                    duration_seconds=track.duration_seconds,
                    mime_type=self._settings.stream_mime_type,
                )
            )

            async for chunk in source.stream_track(
                track, chunk_size=self._settings.chunk_size
            ):
                yield chunk

            # маленькая пауза между треками, чтобы не "крутить" цикл на пустом месте
            await asyncio.sleep(0.05)
