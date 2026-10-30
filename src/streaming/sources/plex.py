from __future__ import annotations

import asyncio
import json
import tempfile
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiofiles
import httpx

from src.settings import Settings


class PlexError(RuntimeError):
    """Safe public error codes: never include URLs, headers or credentials."""


@dataclass(frozen=True)
class PlexTrack:
    id: str
    title: str
    album_id: str
    album: str
    artist: str
    disc: int
    number: int
    duration: float
    part_key: str

    def public(self):
        return {k: v for k, v in asdict(self).items() if k != "part_key"}


class PlexSource:
    id = "plex"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.plex_timeout_seconds),
            follow_redirects=False,
        )
        self._temporary = tempfile.TemporaryDirectory(prefix="yurets-plex-")
        self._cache: OrderedDict[str, Path] = OrderedDict()
        self._prepare_lock = asyncio.Lock()

    def configuration_error(self) -> str | None:
        if not (
            self.settings.plex_url
            and self.settings.plex_token.get_secret_value()
            and self.settings.plex_library_id
        ):
            return "plex_not_configured"
        url = urlsplit(self.settings.plex_url)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            return "plex_url_invalid"
        if not self.settings.plex_library_id.isdecimal():
            return "plex_library_invalid"
        return None

    def _url(self, path: str) -> str:
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed.netloc
            or parsed.scheme
            or ".." in parsed.path.split("/")
            or "\\" in path
        ):
            raise PlexError("plex_media_path_invalid")
        return self.settings.plex_url.rstrip("/") + path

    def _headers(self):
        return {
            "Accept": "application/json",
            "X-Plex-Token": self.settings.plex_token.get_secret_value(),
            "X-Plex-Client-Identifier": "yurets-fm-radio",
            "X-Plex-Product": "Yurets",
        }

    @staticmethod
    def _check(response: httpx.Response):
        if response.status_code in {401, 403}:
            raise PlexError("plex_access_denied")
        if response.status_code != 200:
            raise PlexError("plex_request_failed")

    async def _get(self, path: str, params=None):
        try:
            response = await self.client.get(
                self._url(path), params=params, headers=self._headers()
            )
            self._check(response)
            data = response.json()["MediaContainer"]
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise PlexError("plex_unavailable") from exc

    async def catalog(self) -> list[PlexTrack]:
        error = self.configuration_error()
        if error:
            raise PlexError(error)
        if self.settings.plex_server_id:
            identity = await self._get("/identity")
            if identity.get("machineIdentifier") != self.settings.plex_server_id:
                raise PlexError("plex_server_mismatch")
        sections = await self._get("/library/sections")
        if not any(
            str(s.get("key")) == self.settings.plex_library_id and s.get("type") == "artist"
            for s in sections.get("Directory", [])
        ):
            raise PlexError("plex_music_library_not_found")
        tracks: dict[str, PlexTrack] = {}
        offset = 0
        while True:
            data = await self._get(
                f"/library/sections/{self.settings.plex_library_id}/all",
                {
                    "type": "10",
                    "X-Plex-Container-Start": offset,
                    "X-Plex-Container-Size": 500,
                },
            )
            items = data.get("Metadata", [])
            for item in items:
                try:
                    # Parts from one media edition only; multi-part tracks are unsupported.
                    media = next(m for m in item.get("Media", []) if len(m.get("Part", [])) == 1)
                    part = media["Part"][0]["key"]
                    if not part.startswith("/library/parts/"):
                        continue
                    self._url(part)
                    duration = float(item["duration"]) / 1000
                    if not 0 < duration <= 86400:
                        continue
                    track = PlexTrack(
                        id=str(item["ratingKey"]),
                        title=item["title"],
                        album_id=str(item["parentRatingKey"]),
                        album=item.get("parentTitle", "Альбом"),
                        artist=item.get("grandparentTitle", "Исполнитель"),
                        disc=int(item.get("parentIndex", 1)),
                        number=int(item.get("index", 0)),
                        duration=duration,
                        part_key=part,
                    )
                    tracks[track.id] = track
                except (KeyError, TypeError, ValueError, StopIteration):
                    continue
            offset += len(items)
            if not items or offset >= int(data.get("totalSize", offset)):
                break
            if offset > 1_000_000:
                raise PlexError("plex_catalog_too_large")
        if not tracks:
            raise PlexError("plex_library_empty")
        return sorted(tracks.values(), key=lambda t: (t.album_id, t.disc, t.number, t.id))

    async def prepare(self, track: PlexTrack) -> Path:
        # A single converter bounds CPU/disk use; the streamer prefetches one track.
        async with self._prepare_lock:
            key = json.dumps(asdict(track), sort_keys=True)
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            root = Path(self._temporary.name)
            with tempfile.NamedTemporaryFile(dir=root, suffix=".source", delete=False) as f:
                original = Path(f.name)
            encoded = original.with_suffix(".mp3")
            process = None
            try:
                async with self.client.stream(
                    "GET", self._url(track.part_key), headers=self._headers()
                ) as response:
                    self._check(response)
                    size = 0
                    async with aiofiles.open(original, "wb") as output:
                        async for chunk in response.aiter_bytes(65536):
                            size += len(chunk)
                            if size > self.settings.plex_max_track_bytes:
                                raise PlexError("plex_track_too_large")
                            await output.write(chunk)
                process = await asyncio.create_subprocess_exec(
                    self.settings.ffmpeg_path,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(original),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-map_metadata",
                    "-1",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    f"{self.settings.assumed_bitrate_kbps}k",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-write_xing",
                    "0",
                    "-id3v2_version",
                    "0",
                    str(encoded),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(process.wait(), timeout=300)
                if process.returncode != 0 or not encoded.exists() or encoded.stat().st_size == 0:
                    raise PlexError("plex_transcode_failed")
                self._cache[key] = encoded
                while len(self._cache) > 3:
                    _, stale = self._cache.popitem(last=False)
                    stale.unlink(missing_ok=True)
                return encoded
            except (httpx.HTTPError, OSError, TimeoutError) as exc:
                raise PlexError("plex_media_unavailable") from exc
            finally:
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
                original.unlink(missing_ok=True)
                if key not in self._cache:
                    encoded.unlink(missing_ok=True)

    async def stream_track(self, track: PlexTrack, offset: float = 0):
        path = await self.prepare(track)
        process = await asyncio.create_subprocess_exec(
            self.settings.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(max(0, offset)),
            "-i",
            str(path),
            "-c:a",
            "copy",
            "-map_metadata",
            "-1",
            "-write_xing",
            "0",
            "-id3v2_version",
            "0",
            "-f",
            "mp3",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while chunk := await process.stdout.read(self.settings.broadcast_chunk_size):
                yield chunk
            if await process.wait() != 0:
                raise PlexError("plex_stream_failed")
        finally:
            if process.returncode is None:
                process.kill()
            # Drain the pipe even if FFmpeg already exited. A paused reader can
            # otherwise keep its transport alive after cancellation/shutdown.
            await process.communicate()

    async def close(self):
        await self.client.aclose()
        self._temporary.cleanup()
