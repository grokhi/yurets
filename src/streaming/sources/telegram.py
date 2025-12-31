from __future__ import annotations

import random
import time as time_module
from dataclasses import dataclass
from typing import AsyncIterator

from telethon import TelegramClient

from src.settings import TelegramSettings
from src.streaming.sources.base import TrackRef


@dataclass(frozen=True)
class _TelegramTrack:
    client: TelegramClient
    media: object


class TelegramChannelSource:
    id = "telegram"

    def __init__(self, settings: TelegramSettings) -> None:
        self._settings = settings
        self._client: TelegramClient | None = None
        self._candidates: list[TrackRef] = []
        self._cache_ts: float = 0.0

    async def startup(self) -> None:
        if not self._settings.api_id or not self._settings.api_hash:
            return

        self._client = TelegramClient(
            self._settings.session, self._settings.api_id, self._settings.api_hash
        )

        if self._settings.bot_token:
            await self._client.start(bot_token=self._settings.bot_token)
        else:
            # Без bot token Telethon обычно просит интерактивный логин.
            # Для прототипа в Docker рекомендуется использовать bot token.
            await self._client.start()

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    def enabled(self) -> bool:
        return bool(
            self._settings.api_id and self._settings.api_hash and self._settings.channel
        )

    async def next_track(self, mime_type: str) -> TrackRef:
        if not self.enabled() or self._client is None:
            raise RuntimeError("Telegram source is not configured")

        await self._refresh_candidates_if_needed(mime_type=mime_type)
        if not self._candidates:
            raise RuntimeError("No suitable audio messages found in Telegram channel")

        return random.choice(self._candidates)

    async def stream_track(
        self, track: TrackRef, chunk_size: int
    ) -> AsyncIterator[bytes]:
        tg: _TelegramTrack = track.ref  # type: ignore[assignment]
        async for chunk in tg.client.iter_download(tg.media, chunk_size=chunk_size):
            if not chunk:
                continue
            yield bytes(chunk)

    async def _refresh_candidates_if_needed(self, mime_type: str) -> None:
        now = time_module.time()
        if now - self._cache_ts < 15 and self._candidates:
            return

        exts = _extensions_for_mime(mime_type)

        assert self._client is not None
        assert self._settings.channel is not None

        candidates: list[TrackRef] = []

        async for msg in self._client.iter_messages(
            self._settings.channel, limit=self._settings.fetch_limit
        ):
            if not getattr(msg, "file", None):
                continue

            name = getattr(msg.file, "name", None) or ""
            if not any(name.lower().endswith(ext) for ext in exts):
                continue

            duration = None
            if getattr(msg, "audio", None) is not None:
                duration = getattr(msg.audio, "duration", None)

            title = name or (
                getattr(msg, "message", None) or f"Telegram track {msg.id}"
            )
            candidates.append(
                TrackRef(
                    title=title,
                    duration_seconds=(
                        int(duration) if isinstance(duration, (int, float)) else None
                    ),
                    ref=_TelegramTrack(client=self._client, media=msg.media),
                )
            )

        self._candidates = candidates
        self._cache_ts = now


def _extensions_for_mime(mime_type: str) -> set[str]:
    if mime_type == "audio/ogg":
        return {".ogg", ".opus"}
    return {".mp3"}
