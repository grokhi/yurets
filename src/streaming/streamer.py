from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from src.models.now_playing import NowPlaying
from src.services.now_playing import NowPlayingState
from src.services.scheduler import Scheduler
from src.settings import Settings
from src.streaming.sources.local import LocalLibrarySource
from src.streaming.sources.telegram import TelegramChannelSource, TelegramSession

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_ROOT = Path("/music")


class Streamer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.now_playing = NowPlayingState()
        self.scheduler = Scheduler(slots=settings.schedule())

        self._local_sources: dict[Path, LocalLibrarySource] = {}
        self.telegram_session = TelegramSession(settings=settings.telegram())
        self._telegram_sources: dict[str, TelegramChannelSource] = {}

    async def startup(self) -> None:
        # Start Telegram session only if schedule uses it.
        if any(slot.source == "telegram" for slot in self._settings.schedule()):
            await self.telegram_session.startup()

    async def shutdown(self) -> None:
        await self.telegram_session.shutdown()

    def _choose_slot(self):
        return self.scheduler.choose_slot(datetime.now())

    def _get_local_source(self, music_dir: Path) -> LocalLibrarySource:
        src = self._local_sources.get(music_dir)
        if src is None:
            src = LocalLibrarySource(music_dir=music_dir)
            self._local_sources[music_dir] = src
        return src

    def _get_telegram_source(self, channel: str) -> TelegramChannelSource:
        src = self._telegram_sources.get(channel)
        if src is None:
            src = TelegramChannelSource(session=self.telegram_session, channel=channel)
            self._telegram_sources[channel] = src
        return src

    async def stream(self) -> AsyncIterator[bytes]:
        # Непрерывный поток: по треку за раз, бесконечно.
        while True:
            source_label = "unknown"
            try:
                slot = self._choose_slot()
                if slot is None:
                    raise RuntimeError("Schedule is empty")

                if slot.source == "telegram":
                    channel = (slot.key or "").strip()
                    if not channel:
                        raise RuntimeError("Schedule slot for telegram must include key=<channel>")
                    source = self._get_telegram_source(channel)
                    source_label = await source.display_name()

                elif slot.source == "local":
                    music_key = (slot.key or "").strip()
                    if not music_key:
                        raise RuntimeError("Schedule slot for local must include key=<path>")

                    key_path = Path(music_key)
                    music_dir = (
                        key_path if key_path.is_absolute() else (DEFAULT_LOCAL_ROOT / key_path)
                    )
                    source = self._get_local_source(music_dir)
                    source_label = str(music_dir.name)

                else:
                    raise RuntimeError(f"Unknown source: {slot.source!r}")

                track = await source.next_track(mime_type=self._settings.stream_mime_type)

                await self.now_playing.set(
                    NowPlaying(
                        title=track.title,
                        source=source_label,
                        duration_seconds=track.duration_seconds,
                        mime_type=self._settings.stream_mime_type,
                    )
                )

                async for chunk in source.stream_track(track, chunk_size=self._settings.chunk_size):
                    yield chunk

                # маленькая пауза между треками, чтобы не "крутить" цикл на пустом месте
                await asyncio.sleep(0.05)
            except Exception:
                # Keep the stream alive on misconfig / temporary source issues.
                logger.exception("Source error (source_label=%s)", source_label)
                await self.now_playing.set(
                    NowPlaying(
                        title="(ошибка источника)",
                        source=source_label,
                        duration_seconds=None,
                        mime_type=self._settings.stream_mime_type,
                    )
                )
                await asyncio.sleep(1.0)
