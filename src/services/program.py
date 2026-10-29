from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.services.database import Database
from src.settings import Settings
from src.streaming.sources.plex import PlexError, PlexSource, PlexTrack


@dataclass(frozen=True)
class ProgramEntry:
    track: PlexTrack
    start: float
    end: float
    block: str

    def public(self):
        return {
            **self.track.public(),
            "start_at": datetime.fromtimestamp(self.start, ZoneInfo("UTC")).isoformat(),
            "end_at": datetime.fromtimestamp(self.end, ZoneInfo("UTC")).isoformat(),
        }


class DailyProgram:
    """The persisted day plan is the sole source of playback and queue order."""

    def __init__(self, settings: Settings, source: PlexSource, database: Database):
        self.settings, self.source, self.database = settings, source, database
        self.timezone = ZoneInfo(settings.timezone)
        self._lock = asyncio.Lock()
        self._catalog_day: date | None = None
        self._catalog: list[PlexTrack] = []
        self._plans: dict[date, list[ProgramEntry]] = {}
        identity = [
            settings.plex_url,
            settings.plex_server_id,
            settings.plex_library_id,
            settings.timezone,
            settings.plex_album_ids,
        ]
        self.namespace = hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def _load(self, day: date) -> list[ProgramEntry] | None:
        if day in self._plans:
            return self._plans[day]
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT entries FROM daily_plans WHERE namespace=? AND day=?",
                (self.namespace, day.isoformat()),
            ).fetchone()
        if row is None:
            return None
        entries = [
            ProgramEntry(PlexTrack(**item["track"]), item["start"], item["end"], item["block"])
            for item in json.loads(row["entries"])
        ]
        self._plans[day] = entries
        return entries

    async def _ensure(self, day: date) -> list[ProgramEntry]:
        saved = self._load(day)
        if saved is not None:
            return saved
        if self._catalog_day != day:
            self._catalog = await self.source.catalog()
            self._catalog_day = day
        albums: dict[str, list[PlexTrack]] = {}
        for track in self._catalog:
            albums.setdefault(track.album_id, []).append(track)
        for tracks in albums.values():
            tracks.sort(key=lambda t: (t.disc, t.number, t.id))
        if self.settings.plex_album_ids:
            order = list(dict.fromkeys(self.settings.plex_album_ids))
            if any(key not in albums for key in order):
                raise PlexError("plex_scheduled_album_missing")
        else:
            order = sorted(albums)
            seed = hashlib.sha256(f"{self.namespace}:{day}".encode()).hexdigest()
            random.Random(seed).shuffle(order)
        if not order or any(sum(t.duration for t in albums[key]) > 86400 for key in order):
            raise PlexError("plex_album_duration_invalid")
        start = datetime.combine(day, time(), tzinfo=self.timezone).timestamp()
        finish = datetime.combine(day + timedelta(days=1), time(), tzinfo=self.timezone).timestamp()
        previous = self._load(day - timedelta(days=1))
        # Finish the previous day's last album, even when it crosses midnight.
        if previous:
            start = max(start, previous[-1].end)
        result = []
        block = 0
        while start < finish:
            for track in albums[order[block % len(order)]]:
                result.append(ProgramEntry(track, start, start + track.duration, f"{day}:{block}"))
                start += track.duration
            block += 1
        with self.database.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO daily_plans VALUES (?, ?, ?)",
                (self.namespace, day.isoformat(), json.dumps([asdict(e) for e in result])),
            )
            # Keep a week of program history; listener history has separate retention.
            db.execute(
                "DELETE FROM daily_plans WHERE day < ?", ((day - timedelta(days=7)).isoformat(),)
            )
        self._plans = {d: p for d, p in self._plans.items() if abs((d - day).days) <= 2}
        # Read the canonical saved plan if another process raced with generation.
        return self._load(day) or []

    async def entries(self, now: datetime, *, tomorrow: bool = False) -> list[ProgramEntry]:
        day = now.astimezone(self.timezone).date()
        async with self._lock:
            today = await self._ensure(day)
            start = datetime.combine(day, time(), tzinfo=self.timezone).timestamp()
            previous = [e for e in (self._load(day - timedelta(days=1)) or []) if e.end > start]
            following = await self._ensure(day + timedelta(days=1)) if tomorrow else []
            return previous + today + following

    async def current(self, now: datetime) -> ProgramEntry | None:
        stamp = now.timestamp()
        return next((e for e in await self.entries(now) if e.start <= stamp < e.end), None)

    async def upcoming(self, now: datetime, count: int) -> list[ProgramEntry]:
        if count <= 0:
            return []
        entries = await self.entries(now)
        result = [e for e in entries if e.start > now.timestamp()]
        if len(result) < count:
            entries = await self.entries(now, tomorrow=True)
            result = [e for e in entries if e.start > now.timestamp()]
        return result[:count]

    async def schedule(self, now: datetime):
        entries = await self.entries(now)
        blocks: dict[str, dict] = {}
        for entry in entries:
            public = entry.public()
            if entry.block not in blocks:
                blocks[entry.block] = {
                    "album_id": entry.track.album_id,
                    "album": entry.track.album,
                    "artist": entry.track.artist,
                    "source": "Plex",
                    "tracks": 0,
                    "start_at": public["start_at"],
                }
            blocks[entry.block]["end_at"] = public["end_at"]
            blocks[entry.block]["tracks"] += 1
        return {
            "date": now.astimezone(self.timezone).date().isoformat(),
            "timezone": self.settings.timezone,
            "slots": list(blocks.values()),
        }
