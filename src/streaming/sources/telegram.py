from __future__ import annotations

import random
import sys
import time as time_module
from dataclasses import dataclass
from typing import Any, AsyncIterator, cast

from telethon import TelegramClient  # type: ignore[import-untyped]

from src.settings import TelegramSettings
from src.streaming.sources.base import TrackRef


@dataclass(frozen=True)
class _TelegramTrack:
    client: TelegramClient
    media: object


class TelegramSession:
    def __init__(self, settings: TelegramSettings) -> None:
        self._settings = settings
        self._client: TelegramClient | None = None
        self._channel_labels: dict[str, str] = {}

    def configured(self) -> bool:
        return bool(self._settings.api_id and self._settings.api_hash)

    def enabled(self) -> bool:
        return bool(self.configured() and self._client is not None)

    @property
    def fetch_limit(self) -> int:
        return self._settings.fetch_limit

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise RuntimeError("Telegram session is not started")
        return self._client

    async def startup(self) -> None:
        if not self.configured():
            return

        api_id = cast(int, self._settings.api_id)
        api_hash = cast(str, self._settings.api_hash)
        client = TelegramClient(self._settings.session, api_id, api_hash)

        # 1) Bot auth (non-interactive)
        if self._settings.bot_token:
            await client.start(bot_token=self._settings.bot_token)  # type: ignore[misc]
            self._client = client
            return

        # 2) User session auth.
        # If a valid session already exists, Telethon won't need to prompt.
        # If not authorized yet and stdin is not interactive (e.g. Docker), skip Telegram.
        await client.connect()
        if await client.is_user_authorized():
            self._client = client
            return

        if not sys.stdin.isatty():
            await client.disconnect()  # type: ignore[misc]
            return

        # Interactive login (phone + code). Use `python -m src.telegram_login` once.
        await client.start()  # type: ignore[misc]
        self._client = client

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.disconnect()  # type: ignore[misc]
            self._client = None
        self._channel_labels.clear()

    async def display_name(self, channel: str) -> str:
        channel = (channel or "").strip()
        if not channel:
            return "telegram"

        cached = self._channel_labels.get(channel)
        if cached:
            return cached

        if not self.enabled():
            return channel

        label = await self._resolve_channel_label(channel)
        self._channel_labels[channel] = label
        return label

    async def _resolve_channel_label(self, channel: str) -> str:
        try:
            entity = await self.client.get_entity(channel)
            title = getattr(entity, "title", None)
            if isinstance(title, str) and title.strip():
                return title.strip()

            username = getattr(entity, "username", None)
            if isinstance(username, str) and username.strip():
                return "@" + username.strip().lstrip("@")
        except Exception:
            pass

        return channel


class TelegramChannelSource:
    id = "telegram"

    def __init__(self, session: TelegramSession, channel: str) -> None:
        self._session = session
        self._channel = channel
        self._candidates: list[TrackRef] = []
        self._cache_ts: float = 0.0

    @property
    def channel(self) -> str:
        return self._channel

    def enabled(self) -> bool:
        return self._session.enabled()

    async def display_name(self) -> str:
        return await self._session.display_name(self._channel)

    async def next_track(self, mime_type: str, rng: random.Random | None = None) -> TrackRef:
        if not self.enabled():
            raise RuntimeError("Telegram session is not configured")
        if not self._channel:
            raise RuntimeError("Telegram channel key is missing in schedule")

        await self._refresh_candidates_if_needed(mime_type=mime_type)
        if not self._candidates:
            raise RuntimeError("No suitable audio messages found in Telegram channel")

        chooser = rng or random
        return chooser.choice(self._candidates)

    async def stream_track(self, track: TrackRef, chunk_size: int) -> AsyncIterator[bytes]:
        tg: _TelegramTrack = track.ref  # type: ignore[assignment]
        async for chunk in tg.client.iter_download(tg.media, chunk_size=chunk_size):  # type: ignore[arg-type]
            if not chunk:
                continue
            yield bytes(chunk)  # type: ignore[arg-type]

    async def _refresh_candidates_if_needed(self, mime_type: str) -> None:
        now = time_module.time()
        if now - self._cache_ts < 15 and self._candidates:
            return

        exts = _extensions_for_mime(mime_type)
        client = self._session.client
        channel = self._channel

        candidates: list[TrackRef] = []

        async for msg in client.iter_messages(channel, limit=self._session.fetch_limit):  # type: ignore[misc]
            msg = cast(Any, msg)
            if not getattr(msg, "file", None):
                continue

            name = getattr(msg.file, "name", None) or ""
            if not any(name.lower().endswith(ext) for ext in exts):
                continue

            duration = None
            if getattr(msg, "audio", None) is not None:
                duration = getattr(msg.audio, "duration", None)

            byte_size = getattr(getattr(msg, "file", None), "size", None)

            title = name or (getattr(msg, "message", None) or f"Telegram track {msg.id}")
            candidates.append(
                TrackRef(
                    title=title,
                    duration_seconds=(
                        int(duration) if isinstance(duration, (int, float)) else None
                    ),
                    byte_size=(int(byte_size) if isinstance(byte_size, (int, float)) else None),
                    ref=_TelegramTrack(client=client, media=msg.media),
                )
            )

        self._candidates = candidates
        self._cache_ts = now


def _extensions_for_mime(mime_type: str) -> set[str]:
    if mime_type == "audio/ogg":
        return {".ogg", ".opus"}
    return {".mp3"}
