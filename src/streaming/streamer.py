from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time as time_module
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.models.now_playing import NowPlaying
from src.services.now_playing import NowPlayingState
from src.services.scheduler import Scheduler
from src.settings import Settings
from src.streaming.sources.local import LocalLibrarySource
from src.streaming.sources.telegram import TelegramChannelSource, TelegramSession

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_ROOT = Path("/music")

_SENTINEL = object()


class Streamer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._schedule_tz_name = settings.schedule_timezone()
        self._schedule_tz = self._build_tz(self._schedule_tz_name)
        self.now_playing = NowPlayingState()
        self.scheduler = Scheduler(slots=settings.schedule())

        self._local_sources: dict[Path, LocalLibrarySource] = {}
        self.telegram_session = TelegramSession(settings=settings.telegram())
        self._telegram_sources: dict[str, TelegramChannelSource] = {}

        self._master_task: asyncio.Task[None] | None = None
        self._subscribers: dict[int, asyncio.Queue[object]] = {}
        self._subscriber_seq = 0

        self._current_day: date | None = None
        self._slot_rngs: dict[tuple[str, str], random.Random] = {}

        self._track_started_at: float | None = None

        self._stats_started_at = time_module.monotonic()
        self._subscribers_created_total = 0
        self._subscribers_dropped_total = 0
        self._subscribers_peak = 0
        self._subscriber_queue_full_total = 0

        self._broadcast_chunks_total = 0
        self._broadcast_bytes_total = 0
        self._last_broadcast_at: float | None = None

        self._master_loops_total = 0
        self._last_master_error: str | None = None
        self._last_master_error_at: float | None = None

        # Current-track diagnostics
        self._track_source_kind: str | None = None
        self._track_key: str | None = None
        self._track_label: str | None = None
        self._track_title: str | None = None
        self._track_kbps_used: int | None = None
        self._track_pace_mode: str | None = None
        self._track_bytes_per_second: float | None = None
        self._track_sent_bytes = 0
        self._track_sent_chunks = 0
        self._track_sleep_total_s = 0.0
        self._track_late_max_s = 0.0
        self._track_source_gap_max_s = 0.0
        self._track_source_gap_over_250ms = 0
        self._track_starve_max_s = 0.0
        self._track_starve_over_250ms = 0
        self._track_buffer_peak_chunks = 0
        self._track_buffer_last_chunks = 0

    async def startup(self) -> None:
        # Start Telegram session only if schedule uses it.
        if any(slot.source == "telegram" for slot in self._settings.schedule()):
            await self.telegram_session.startup()

        if self._master_task is None:
            self._master_task = asyncio.create_task(self._run_master(), name="yurets-master-stream")

    async def shutdown(self) -> None:
        if self._master_task is not None:
            self._master_task.cancel()
            try:
                await self._master_task
            except asyncio.CancelledError:
                pass
            finally:
                self._master_task = None

        # Close subscribers
        for q in list(self._subscribers.values()):
            try:
                q.put_nowait(_SENTINEL)
            except Exception:
                pass
        self._subscribers.clear()

        await self.telegram_session.shutdown()

    def _choose_slot(self):
        return self.scheduler.choose_slot(datetime.now(self._schedule_tz))

    @staticmethod
    def _build_tz(name: str):
        # Fast path for UTC
        if not name or name.upper() in {"UTC", "Z"}:
            return timezone.utc
        # Fixed offsets like +03:00 / -0500 / +0300
        m = re.match(r"^([+-])(\d{2}):?(\d{2})$", name.strip())
        if m:
            sign = 1 if m.group(1) == "+" else -1
            hh = int(m.group(2))
            mm = int(m.group(3))
            return timezone(sign * timedelta(hours=hh, minutes=mm))
        return ZoneInfo(name)

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

    def subscribe(self) -> AsyncIterator[bytes]:
        """Subscribe to the master stream.

        All clients receive the same chunks in real time.
        New clients join mid-track (typical radio behavior).
        """

        self._subscriber_seq += 1
        subscriber_id = self._subscriber_seq
        self._subscribers_created_total += 1
        q: asyncio.Queue[object] = asyncio.Queue(
            maxsize=int(self._settings.subscriber_queue_chunks)
        )
        self._subscribers[subscriber_id] = q
        if len(self._subscribers) > self._subscribers_peak:
            self._subscribers_peak = len(self._subscribers)

        async def _gen() -> AsyncIterator[bytes]:
            try:
                while True:
                    item = await q.get()
                    if item is _SENTINEL:
                        return
                    if isinstance(item, (bytes, bytearray)):
                        yield bytes(item)
            finally:
                self._subscribers.pop(subscriber_id, None)

        return _gen()

    async def _run_master(self) -> None:
        """Background task that selects tracks and broadcasts bytes to subscribers."""

        while True:
            source_label = "unknown"
            try:
                self._master_loops_total += 1
                slot = self._choose_slot()
                if slot is None:
                    raise RuntimeError("Schedule is empty")

                # reset daily RNGs on day change
                today = datetime.now(self._schedule_tz).date()
                if self._current_day != today:
                    self._current_day = today
                    self._slot_rngs.clear()

                if slot.source == "telegram":
                    channel = (slot.key or "").strip()
                    if not channel:
                        raise RuntimeError("Schedule slot for telegram must include key=<channel>")
                    tg_source = self._get_telegram_source(channel)
                    source = tg_source
                    source_label = await tg_source.display_name()
                    rng = self._rng_for(slot_source=slot.source, key=channel)

                    self._track_source_kind = "telegram"
                    self._track_key = channel

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
                    rng = self._rng_for(slot_source=slot.source, key=str(music_dir))

                    self._track_source_kind = "local"
                    self._track_key = str(music_dir)

                else:
                    raise RuntimeError(f"Unknown source: {slot.source!r}")

                track = await source.next_track(mime_type=self._settings.stream_mime_type, rng=rng)

                self._track_started_at = time_module.monotonic()

                self._track_label = source_label
                self._track_title = track.title
                self._track_sent_bytes = 0
                self._track_sent_chunks = 0
                self._track_sleep_total_s = 0.0
                self._track_late_max_s = 0.0
                self._track_source_gap_max_s = 0.0
                self._track_source_gap_over_250ms = 0
                self._track_starve_max_s = 0.0
                self._track_starve_over_250ms = 0
                self._track_buffer_peak_chunks = 0
                self._track_buffer_last_chunks = 0

                await self.now_playing.set(
                    NowPlaying(
                        title=track.title,
                        source=source_label,
                        duration_seconds=track.duration_seconds,
                        position_seconds=0,
                        mime_type=self._settings.stream_mime_type,
                    )
                )

                # Pace the broadcast to (roughly) real time.
                bytes_per_second: float | None = None
                if (
                    track.byte_size is not None
                    and track.duration_seconds is not None
                    and track.duration_seconds > 0
                    and track.byte_size > 0
                ):
                    bytes_per_second = float(track.byte_size) / float(track.duration_seconds)
                    self._track_pace_mode = "duration"
                    self._track_kbps_used = None

                if bytes_per_second is None:
                    # Fallback pacing when we don't know real duration.
                    # Try to infer kbps from the filename/title (often contains "(320)").
                    # If we under-estimate bitrate here, clients will eventually underrun and stutter.
                    title_kbps = self._infer_kbps_from_title(track.title)
                    kbps = title_kbps or int(self._settings.assumed_bitrate_kbps)
                    # kbps -> bytes/sec
                    bytes_per_second = max(1.0, float(kbps) * 1000.0 / 8.0)
                    self._track_pace_mode = "title_kbps" if title_kbps else "assumed_kbps"
                    self._track_kbps_used = int(kbps)

                self._track_bytes_per_second = float(bytes_per_second)

                # Pre-buffer source as fast as possible (especially important for Telegram),
                # then broadcast in real time from the buffer.
                buffer_q: asyncio.Queue[bytes | None] = asyncio.Queue(
                    maxsize=int(self._settings.track_buffer_chunks)
                )

                async def _producer() -> None:
                    last_at = time_module.monotonic()
                    try:
                        source_chunk_size = self._settings.chunk_size
                        if getattr(source, "kind", None) == "telegram":
                            source_chunk_size = self._settings.telegram_download_chunk_size

                        async for raw in source.stream_track(track, source_chunk_size):
                            now = time_module.monotonic()
                            gap = now - last_at
                            last_at = now
                            if gap > self._track_source_gap_max_s:
                                self._track_source_gap_max_s = gap
                            if gap >= 0.25:
                                self._track_source_gap_over_250ms += 1

                            await buffer_q.put(raw)
                            qsz = buffer_q.qsize()
                            self._track_buffer_last_chunks = qsz
                            if qsz > self._track_buffer_peak_chunks:
                                self._track_buffer_peak_chunks = qsz
                    finally:
                        try:
                            await buffer_q.put(None)
                        except Exception:
                            pass

                producer_task = asyncio.create_task(_producer(), name="yurets-track-producer")

                t0 = self._track_started_at
                sent = 0
                carry = b""
                bchunk = int(self._settings.broadcast_chunk_size)

                try:
                    while True:
                        if not carry:
                            wait0 = time_module.monotonic()
                            item = await buffer_q.get()
                            waited = time_module.monotonic() - wait0
                            if waited > self._track_starve_max_s:
                                self._track_starve_max_s = waited
                            if waited >= 0.25:
                                self._track_starve_over_250ms += 1

                            if item is None:
                                break
                            carry = item
                            self._track_buffer_last_chunks = buffer_q.qsize()

                        chunk = carry[:bchunk]
                        carry = carry[bchunk:]

                        if not chunk:
                            continue

                        self._broadcast(chunk)
                        sent += len(chunk)

                        self._track_sent_bytes += len(chunk)
                        self._track_sent_chunks += 1

                        target_elapsed = sent / bytes_per_second
                        actual_elapsed = time_module.monotonic() - t0
                        late = actual_elapsed - target_elapsed
                        if late > self._track_late_max_s:
                            self._track_late_max_s = late
                        if target_elapsed > actual_elapsed:
                            sleep_s = target_elapsed - actual_elapsed
                            self._track_sleep_total_s += sleep_s
                            await asyncio.sleep(sleep_s)
                finally:
                    producer_task.cancel()
                    try:
                        await producer_task
                    except asyncio.CancelledError:
                        pass

                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Master source error (source_label=%s)", source_label)
                self._track_started_at = None
                self._last_master_error = "master_error"
                self._last_master_error_at = time_module.monotonic()
                await self.now_playing.set(
                    NowPlaying(
                        title="(ошибка источника)",
                        source=source_label,
                        duration_seconds=None,
                        position_seconds=None,
                        mime_type=self._settings.stream_mime_type,
                    )
                )
                await asyncio.sleep(1.0)

    def current_position_seconds(self) -> int | None:
        if self._track_started_at is None:
            return None
        pos = int(max(0.0, time_module.monotonic() - self._track_started_at))
        return pos

    async def preview_tracks(self, count_per_slot: int) -> list[dict[str, object]]:
        """Preview upcoming deterministic picks per slot.

        Uses a copy of each slot RNG state so the live stream is not affected.
        """

        today = datetime.now(self._schedule_tz).date()
        if self._current_day != today:
            self._current_day = today
            self._slot_rngs.clear()

        out: list[dict[str, object]] = []
        for slot in self._settings.schedule():
            slot_key = (slot.key or "").strip()
            if slot.source == "telegram":
                if not slot_key:
                    out.append({"slot": slot.model_dump(), "error": "missing key"})
                    continue
                source = self._get_telegram_source(slot_key)
                label = await source.display_name() if source.enabled() else slot_key
                live_rng = self._rng_for(slot_source=slot.source, key=slot_key)
            elif slot.source == "local":
                if not slot_key:
                    out.append({"slot": slot.model_dump(), "error": "missing key"})
                    continue
                key_path = Path(slot_key)
                music_dir = key_path if key_path.is_absolute() else (DEFAULT_LOCAL_ROOT / key_path)
                source = self._get_local_source(music_dir)
                label = music_dir.name
                live_rng = self._rng_for(slot_source=slot.source, key=str(music_dir))
            else:
                out.append({"slot": slot.model_dump(), "error": f"unknown source: {slot.source!r}"})
                continue

            rng_copy = random.Random()
            rng_copy.setstate(live_rng.getstate())

            tracks: list[str] = []
            seen: set[str] = set()
            err: str | None = None
            try:
                want = max(0, int(count_per_slot))
                # Random choice is with replacement; to present a usable "queue" we
                # skip duplicates and try a few extra draws.
                max_attempts = max(want + 20, want * 10)
                for _ in range(max_attempts):
                    tr = await source.next_track(
                        mime_type=self._settings.stream_mime_type, rng=rng_copy
                    )
                    title = tr.title
                    if title in seen:
                        continue
                    seen.add(title)
                    tracks.append(title)
                    if len(tracks) >= want:
                        break
            except Exception as exc:
                err = str(exc)

            out.append(
                {
                    "slot": {
                        "start": slot.start.isoformat(),
                        "end": slot.end.isoformat(),
                        "source": slot.source,
                        "key": slot.key,
                        "label": label,
                    },
                    "tracks": tracks,
                    "error": err,
                }
            )

        return out

    async def queue_preview(self, count: int) -> dict[str, object]:
        """Preview upcoming tracks for the currently active slot.

        Uses a copy of the slot RNG so the live stream is not affected.
        """

        slot = self._choose_slot()
        if slot is None:
            return {"slot": None, "tracks": [], "error": "schedule is empty"}

        slot_key = (slot.key or "").strip()
        try:
            if slot.source == "telegram":
                if not slot_key:
                    return {"slot": slot.model_dump(), "tracks": [], "error": "missing key"}
                source = self._get_telegram_source(slot_key)
                label = await source.display_name() if source.enabled() else slot_key
                live_rng = self._rng_for(slot_source=slot.source, key=slot_key)
            elif slot.source == "local":
                if not slot_key:
                    return {"slot": slot.model_dump(), "tracks": [], "error": "missing key"}
                key_path = Path(slot_key)
                music_dir = key_path if key_path.is_absolute() else (DEFAULT_LOCAL_ROOT / key_path)
                source = self._get_local_source(music_dir)
                label = music_dir.name
                live_rng = self._rng_for(slot_source=slot.source, key=str(music_dir))
            else:
                return {
                    "slot": slot.model_dump(),
                    "tracks": [],
                    "error": f"unknown source: {slot.source!r}",
                }

            rng_copy = random.Random()
            rng_copy.setstate(live_rng.getstate())

            current = await self.now_playing.get()
            current_title = current.title if current is not None else None

            tracks: list[str] = []
            seen: set[str] = set()
            if current_title:
                seen.add(str(current_title))

            want = max(0, int(count))
            # Generate extra to avoid duplicates.
            max_attempts = max(want + 20, want * 10)
            for _ in range(max_attempts):
                tr = await source.next_track(
                    mime_type=self._settings.stream_mime_type, rng=rng_copy
                )
                title = tr.title
                if title in seen:
                    continue
                seen.add(title)
                tracks.append(title)
                if len(tracks) >= want:
                    break

            return {
                "slot": {
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "source": slot.source,
                    "key": slot.key,
                    "label": label,
                },
                "tracks": tracks,
                "error": None,
            }
        except Exception as exc:
            return {
                "slot": {
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "source": slot.source,
                    "key": slot.key,
                },
                "tracks": [],
                "error": str(exc),
            }

    def _broadcast(self, chunk: bytes) -> None:
        # Protect memory: if a subscriber is slow, drop the subscriber.
        # Dropping bytes in the middle of MP3/OGG corrupts the stream and causes audible stutter.
        self._broadcast_chunks_total += 1
        self._broadcast_bytes_total += len(chunk)
        self._last_broadcast_at = time_module.monotonic()
        for sid, q in list(self._subscribers.items()):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                self._subscriber_queue_full_total += 1
                self._close_subscriber(sid, reason="queue_full")
            except Exception:
                self._close_subscriber(sid, reason="exception")

    def _close_subscriber(self, sid: int, reason: str) -> None:
        q = self._subscribers.pop(sid, None)
        if q is None:
            return
        self._subscribers_dropped_total += 1
        try:
            while True:
                q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(_SENTINEL)
        except Exception:
            pass

    def diagnostics(self) -> dict[str, Any]:
        now = time_module.monotonic()
        track_age_s = (now - self._track_started_at) if self._track_started_at else None

        return {
            "uptime_seconds": int(max(0.0, now - self._stats_started_at)),
            "subscribers": {
                "current": len(self._subscribers),
                "peak": self._subscribers_peak,
                "created_total": self._subscribers_created_total,
                "dropped_total": self._subscribers_dropped_total,
                "queue_full_total": self._subscriber_queue_full_total,
                "queue_max_chunks": int(self._settings.subscriber_queue_chunks),
            },
            "broadcast": {
                "chunks_total": self._broadcast_chunks_total,
                "bytes_total": self._broadcast_bytes_total,
                "last_broadcast_ago_ms": (
                    int(1000 * (now - self._last_broadcast_at))
                    if self._last_broadcast_at is not None
                    else None
                ),
                "source_chunk_size": int(self._settings.chunk_size),
                "broadcast_chunk_size": int(self._settings.broadcast_chunk_size),
                "track_buffer_chunks": int(self._settings.track_buffer_chunks),
            },
            "master": {
                "loops_total": self._master_loops_total,
                "last_error": self._last_master_error,
                "last_error_ago_ms": (
                    int(1000 * (now - self._last_master_error_at))
                    if self._last_master_error_at is not None
                    else None
                ),
            },
            "track": {
                "age_seconds": (int(track_age_s) if track_age_s is not None else None),
                "source": self._track_source_kind,
                "key": self._track_key,
                "label": self._track_label,
                "title": self._track_title,
                "pace_mode": self._track_pace_mode,
                "kbps_used": self._track_kbps_used,
                "bytes_per_second": self._track_bytes_per_second,
                "sent_bytes": self._track_sent_bytes,
                "sent_chunks": self._track_sent_chunks,
                "sleep_total_ms": int(1000 * self._track_sleep_total_s),
                "late_max_ms": int(1000 * self._track_late_max_s),
                "source_gap_max_ms": int(1000 * self._track_source_gap_max_s),
                "source_gap_over_250ms": self._track_source_gap_over_250ms,
                "starve_max_ms": int(1000 * self._track_starve_max_s),
                "starve_over_250ms": self._track_starve_over_250ms,
                "buffer_last_chunks": self._track_buffer_last_chunks,
                "buffer_peak_chunks": self._track_buffer_peak_chunks,
            },
        }

    def _rng_for(self, slot_source: str, key: str) -> random.Random:
        day = self._current_day or datetime.now(self._schedule_tz).date()
        cache_key = (slot_source, key)
        rng = self._slot_rngs.get(cache_key)
        if rng is not None:
            return rng

        seed = self._stable_seed(str(day.isoformat()), slot_source, key)
        rng = random.Random(seed)
        self._slot_rngs[cache_key] = rng
        return rng

    @staticmethod
    def _stable_seed(*parts: str) -> int:
        h = hashlib.sha256("\0".join(parts).encode("utf-8")).digest()
        return int.from_bytes(h[:8], "big", signed=False)

    @staticmethod
    def _infer_kbps_from_title(title: str) -> int | None:
        """Best-effort bitrate inference from track title.

        Common patterns:
        - "(... (320).mp3)"
        - "... 320 kbps ..."
        """

        t = (title or "").lower()
        m = re.search(r"\((\d{2,3})\)(?:\D|$)", t)
        if m:
            try:
                val = int(m.group(1))
                if 32 <= val <= 512:
                    return val
            except Exception:
                pass

        m = re.search(r"\b(\d{2,3})\s*kbps\b", t)
        if m:
            try:
                val = int(m.group(1))
                if 32 <= val <= 512:
                    return val
            except Exception:
                pass

        return None
