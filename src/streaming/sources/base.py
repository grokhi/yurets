from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True)
class TrackRef:
    title: str
    duration_seconds: int | None
    ref: object


class MusicSource(Protocol):
    id: str

    async def next_track(self, mime_type: str) -> TrackRef:
        ...

    async def stream_track(self, track: TrackRef, chunk_size: int) -> AsyncIterator[bytes]:
        ...
