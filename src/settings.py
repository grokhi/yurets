from __future__ import annotations

import json
from datetime import time
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScheduleSlot(BaseModel):
    start: time
    end: time
    source: str
    key: str | None = None


class ScheduleConfig(BaseModel):
    timezone: str
    slots: list[ScheduleSlot]


class TelegramSettings(BaseModel):
    api_id: int | None = None
    api_hash: str | None = None
    bot_token: str | None = None
    session: str = "/telegram_session/yurets_fm.session"
    fetch_limit: int = 50


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YURETS_",
        env_file=(".env", ".env.plex"),
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # The active radio pipeline is Plex -> FFmpeg -> constant-bitrate MP3.
    stream_mime_type: Literal["audio/mpeg"] = "audio/mpeg"
    plex_url: str = ""
    plex_token: SecretStr = SecretStr("")
    plex_library_id: str = ""
    plex_server_id: str = ""
    plex_album_ids: list[str] = Field(default_factory=list)
    timezone: str = "Asia/Tokyo"
    data_dir: str = "./data"
    ffmpeg_path: str = "ffmpeg"
    plex_timeout_seconds: float = Field(default=30, gt=0)
    plex_max_track_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    geoip_database: str = str(
        Path(__file__).resolve().parents[1] / "geoip" / "dbip-country-lite.mmdb"
    )
    analytics_retention_days: int = Field(default=90, ge=1)
    secure_cookies: bool = True
    # Chunk size used when reading from local files (and as a default).
    # Telegram downloads can be tuned separately via telegram_download_chunk_size.
    chunk_size: int = 65536

    # Chunk size used when broadcasting to HTTP clients.
    # Smaller chunks reduce jitter/underruns in browser streaming.
    broadcast_chunk_size: int = Field(default=4096, ge=1024, le=65536)
    assumed_bitrate_kbps: int = Field(default=192, ge=64, le=320)

    # How many chunks to buffer per subscriber before dropping the connection.
    # Dropping chunks corrupts MP3/OGG streams and causes audible stutter.
    subscriber_queue_chunks: int = Field(default=256, ge=1, le=2048)

    # How many source-chunks to buffer per track ahead of broadcast.
    # This decouples Telegram download timing from real-time playback.
    track_buffer_chunks: int = 256

    # Chunk size used specifically for Telegram iter_download().
    # Larger values often reduce gaps/stalls from Telegram/CDN.
    telegram_download_chunk_size: int = 262144

    # Schedule format (recommended):
    # {"timezone":"UTC","slots":[{"start":"00:00","end":"00:00","source":"local","key":"/music"}]}
    # Backward-compatible legacy format: a plain list of slots (timezone defaults to UTC).
    schedule_json: str = Field(
        default='{"timezone":"UTC","slots":[{"start":"00:00","end":"00:00","source":"local","key":"/music"}]}'
    )

    # Legacy source configuration is retained for compatibility, but is never
    # read by the active Plex-only streamer. No Telegram client is started.
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

    def schedule_timezone(self) -> str:
        data: Any = json.loads(self.schedule_json)
        if isinstance(data, dict) and "timezone" in data:
            tz = cast(Any, data).get("timezone")
            if isinstance(tz, str) and tz.strip():
                return tz.strip()
        # Legacy schedule list: default to UTC
        return "UTC"

    def schedule(self) -> list[ScheduleSlot]:
        data: Any = json.loads(self.schedule_json)
        if isinstance(data, dict):
            # New format
            cfg = ScheduleConfig.model_validate(data)
            return cfg.slots
        # Legacy format
        return [ScheduleSlot.model_validate(item) for item in data]
