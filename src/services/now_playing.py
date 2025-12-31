from __future__ import annotations

import asyncio

from src.models.now_playing import NowPlaying


class NowPlayingState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current: NowPlaying | None = None

    async def set(self, value: NowPlaying) -> None:
        async with self._lock:
            self._current = value

    async def get(self) -> NowPlaying | None:
        async with self._lock:
            return self._current
