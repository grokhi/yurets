from __future__ import annotations

import random
import time as time_module
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import aiofiles

from src.streaming.sources.base import TrackRef

try:
    from mutagen import File as MutagenFile  # type: ignore
except Exception:  # pragma: no cover
    MutagenFile = None


@dataclass
class _LocalTrack:
    path: Path


class LocalLibrarySource:
    id = "local"

    def __init__(self, music_dir: Path) -> None:
        self._music_dir = music_dir
        self._cache: list[Path] = []
        self._cache_ts: float = 0.0

    async def next_track(self, mime_type: str) -> TrackRef:
        self._refresh_cache_if_needed(mime_type=mime_type)
        if not self._cache:
            raise RuntimeError(f"No audio files found in {self._music_dir}")

        path = random.choice(self._cache)
        title = path.stem
        duration = _try_duration_seconds(path)
        return TrackRef(
            title=title, duration_seconds=duration, ref=_LocalTrack(path=path)
        )

    async def stream_track(
        self, track: TrackRef, chunk_size: int
    ) -> AsyncIterator[bytes]:
        local: _LocalTrack = track.ref  # type: ignore[assignment]
        async with aiofiles.open(local.path, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def _refresh_cache_if_needed(self, mime_type: str) -> None:
        now = time_module.time()
        if now - self._cache_ts < 10 and self._cache:
            return

        exts = _extensions_for_mime(mime_type)
        if not self._music_dir.exists():
            self._cache = []
            self._cache_ts = now
            return

        files: list[Path] = []
        for path in self._music_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() in exts:
                files.append(path)

        self._cache = files
        self._cache_ts = now


def _extensions_for_mime(mime_type: str) -> set[str]:
    if mime_type == "audio/ogg":
        return {".ogg", ".opus"}
    return {".mp3"}


def _try_duration_seconds(path: Path) -> int | None:
    if MutagenFile is None:
        return None
    try:
        mf = MutagenFile(path)
        if mf is None or not hasattr(mf, "info") or mf.info is None:
            return None
        length = getattr(mf.info, "length", None)
        if length is None:
            return None
        return int(length)
    except Exception:
        return None
