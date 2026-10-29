from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing, suppress
from datetime import datetime, timezone

from src.models.now_playing import NowPlaying
from src.services.database import Database
from src.services.now_playing import NowPlayingState
from src.services.program import DailyProgram, ProgramEntry
from src.settings import Settings
from src.streaming.sources.plex import PlexError, PlexSource

logger = logging.getLogger(__name__)
_SENTINEL = object()


class Streamer:
    """One Plex program/encoder shared by every listener, with a bounded fan-out."""

    def __init__(self, settings: Settings, database: Database, source: PlexSource | None = None):
        self.settings = settings
        self.source = source or PlexSource(settings)
        self.program = DailyProgram(settings, self.source, database)
        self.now_playing = NowPlayingState()
        self._master: asyncio.Task | None = None
        self._prefetch: asyncio.Task | None = None
        self._subscribers: dict[int, asyncio.Queue] = {}
        self._sequence = 0
        self._started = time.monotonic()
        self._entry: ProgramEntry | None = None
        self._error: str | None = "starting"
        self._last_broadcast: float | None = None
        self._bytes = 0
        self._chunks = 0
        self._dropped = 0
        self._peak = 0
        self._tracks = 0

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    async def startup(self):
        error = self.source.configuration_error()
        if error:
            self._error = error
            return
        if self._master is None:
            self._master = asyncio.create_task(self._run(), name="yurets-plex-master")

    async def shutdown(self):
        for task in (self._master, self._prefetch):
            if task:
                task.cancel()
        for task in (self._master, self._prefetch):
            if task:
                with suppress(asyncio.CancelledError):
                    await task
        for sid in list(self._subscribers):
            self._close_subscriber(sid)
        await self.source.close()

    async def _prepare_next(self):
        try:
            entries = await self.program.upcoming(self.now(), 1)
            if entries:
                await self.source.prepare(entries[0].track)
        except PlexError:
            # Retry on actual playback; prefetch failure must not stop this track.
            pass

    async def _run(self):
        while True:
            try:
                entry = await self.program.current(self.now())
                if entry is None:
                    raise PlexError("plex_program_empty")
                await self.source.prepare(entry.track)
                offset = max(0, time.time() - entry.start)
                if time.time() >= entry.end:
                    continue
                self._entry = entry
                self._tracks += 1
                track = entry.track
                await self.now_playing.set(
                    NowPlaying(
                        title=track.title,
                        source="Plex",
                        mime_type="audio/mpeg",
                        track_id=track.id,
                        album=track.album,
                        album_id=track.album_id,
                        artist=track.artist,
                        duration_seconds=round(track.duration),
                        position_seconds=int(offset),
                    )
                )
                if self._prefetch:
                    self._prefetch.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._prefetch
                self._prefetch = asyncio.create_task(self._prepare_next())
                started = time.monotonic()
                sent = 0
                bitrate = self.settings.assumed_bitrate_kbps * 1000 / 8
                async with aclosing(self.source.stream_track(track, offset)) as stream:
                    async for chunk in stream:
                        self._broadcast(chunk)
                        self._error = None
                        sent += len(chunk)
                        await asyncio.sleep(max(0, started + sent / bitrate - time.monotonic()))
                # MP3 padding and metadata durations can differ slightly. Do not
                # replay the tail of a track or consume the next queue item early.
                await asyncio.sleep(max(0, entry.end - time.time()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = str(exc) if isinstance(exc, PlexError) else "plex_playback_failed"
                logger.warning("Radio unavailable: %s", self._error)
                self._entry = None
                await self.now_playing.set(
                    NowPlaying(
                        title="Эфир временно недоступен",
                        source="Plex",
                        mime_type="audio/mpeg",
                    )
                )
                await asyncio.sleep(5)

    @property
    def ready(self):
        return (
            self._error is None
            and self._last_broadcast is not None
            and time.monotonic() - self._last_broadcast < 10
        )

    def current_position_seconds(self):
        if self._entry is None:
            return None
        return int(min(self._entry.track.duration, max(0, time.time() - self._entry.start)))

    async def queue_preview(self, count: int):
        entries = await self.program.upcoming(self.now(), count)
        return {"tracks": [e.public() for e in entries], "error": None}

    def subscribe(self):
        async def stream():
            self._sequence += 1
            sid = self._sequence
            queue = asyncio.Queue(maxsize=self.settings.subscriber_queue_chunks)
            self._subscribers[sid] = queue
            self._peak = max(self._peak, len(self._subscribers))
            try:
                while True:
                    item = await queue.get()
                    if item is _SENTINEL:
                        break
                    yield item
            finally:
                self._subscribers.pop(sid, None)

        return stream()

    def _close_subscriber(self, sid):
        queue = self._subscribers.pop(sid, None)
        if queue is not None:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(_SENTINEL)

    def _broadcast(self, chunk: bytes):
        self._last_broadcast = time.monotonic()
        self._chunks += 1
        self._bytes += len(chunk)
        for sid, queue in list(self._subscribers.items()):
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                self._dropped += 1
                self._close_subscriber(sid)

    def diagnostics(self):
        return {
            "source": "plex",
            "ready": self.ready,
            "error": self._error,
            "uptime_seconds": int(time.monotonic() - self._started),
            "subscribers": {
                "current": len(self._subscribers),
                "peak": self._peak,
                "created_total": self._sequence,
                "dropped_total": self._dropped,
            },
            "broadcast": {"bytes_total": self._bytes, "chunks_total": self._chunks},
            "master": {"tracks_started": self._tracks, "last_error": self._error},
        }
