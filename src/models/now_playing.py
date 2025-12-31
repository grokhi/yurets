from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class NowPlaying(BaseModel):
    title: str
    source: str
    duration_seconds: int | None = None
    mime_type: Literal["audio/mpeg", "audio/ogg"]
