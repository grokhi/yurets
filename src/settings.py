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
    chunk_size: int = 65536

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
