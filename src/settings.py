from __future__ import annotations

import json
from datetime import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScheduleSlot(BaseModel):
    start: time
    end: time
    source: str


class TelegramSettings(BaseModel):
    api_id: int | None = None
    api_hash: str | None = None
    bot_token: str | None = None
    channel: str | None = None
    session: str = "/telegram_session/yurets_fm.session"
    fetch_limit: int = 50


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YURETS_",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    stream_mime_type: Literal["audio/mpeg", "audio/ogg"] = "audio/mpeg"
    chunk_size: int = 65536

    local_music_dir: Path = Path("/music")

    schedule_json: str = Field(
        default='[{"start":"00:00","end":"08:00","source":"telegram"},'
        '{"start":"08:00","end":"18:00","source":"local"},'
        '{"start":"18:00","end":"00:00","source":"telegram"}]'
    )

    # Telegram settings (flat env vars with prefix)
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_bot_token: str | None = None
    telegram_channel: str | None = None
    telegram_session: str = "/telegram_session/yurets_fm.session"
    telegram_fetch_limit: int = 50

    def telegram(self) -> TelegramSettings:
        return TelegramSettings(
            api_id=self.telegram_api_id,
            api_hash=self.telegram_api_hash,
            bot_token=self.telegram_bot_token,
            channel=self.telegram_channel,
            session=self.telegram_session,
            fetch_limit=self.telegram_fetch_limit,
        )

    def schedule(self) -> list[ScheduleSlot]:
        data: Any = json.loads(self.schedule_json)
        return [ScheduleSlot.model_validate(item) for item in data]
