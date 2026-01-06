from __future__ import annotations

import json
from datetime import time
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScheduleSlot(BaseModel):
    start: time
    end: time
    source: str
    key: str | None = None


class TelegramSettings(BaseModel):
    api_id: int | None = None
    api_hash: str | None = None
    bot_token: str | None = None
    session: str = "/telegram_session/yurets_fm.session"
    fetch_limit: int = 50


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YURETS_",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    stream_mime_type: Literal["audio/mpeg", "audio/ogg"] = "audio/mpeg"
    # Chunk size used when reading from local files (and as a default).
    # Telegram downloads can be tuned separately via telegram_download_chunk_size.
    chunk_size: int = 65536

    # Chunk size used when broadcasting to HTTP clients.
    # Smaller chunks reduce jitter/underruns in browser streaming.
    broadcast_chunk_size: int = 4096
    assumed_bitrate_kbps: int = 192

    # How many chunks to buffer per subscriber before dropping the connection.
    # Dropping chunks corrupts MP3/OGG streams and causes audible stutter.
    subscriber_queue_chunks: int = 256

    # How many source-chunks to buffer per track ahead of broadcast.
    # This decouples Telegram download timing from real-time playback.
    track_buffer_chunks: int = 256

    # Chunk size used specifically for Telegram iter_download().
    # Larger values often reduce gaps/stalls from Telegram/CDN.
    telegram_download_chunk_size: int = 262144

    schedule_json: str = Field(
        default='[{"start":"00:00","end":"00:00","source":"local","key":"/music"}]'
    )

    # Telegram settings (flat env vars with prefix)
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_bot_token: str | None = None
    telegram_session: str = "/telegram_session/yurets_fm.session"
    telegram_fetch_limit: int = 50

    def telegram(self) -> TelegramSettings:
        return TelegramSettings(
            api_id=self.telegram_api_id,
            api_hash=self.telegram_api_hash,
            bot_token=self.telegram_bot_token,
            session=self.telegram_session,
            fetch_limit=self.telegram_fetch_limit,
        )

    def schedule(self) -> list[ScheduleSlot]:
        data: Any = json.loads(self.schedule_json)
        return [ScheduleSlot.model_validate(item) for item in data]
