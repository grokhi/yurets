import asyncio
import io
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import httpx

from src.settings import Settings
from src.streaming.sources.plex import PlexError, PlexSource, PlexTrack


def item(id="1", **extra):
    return {
        "ratingKey": id,
        "title": "Song",
        "parentRatingKey": "a",
        "parentTitle": "Album",
        "grandparentTitle": "Artist",
        "parentIndex": 1,
        "index": 1,
        "duration": 3000,
        "Media": [{"Part": [{"key": f"/library/parts/{id}/file"}]}],
        **extra,
    }


class PlexTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **kwargs):
        return Settings(
            _env_file=None,
            _env_prefix="YURETS_TEST_",
            plex_url="https://plex.example",
            plex_token="test-credential",
            plex_library_id="1",
            **kwargs,
        )

    async def test_artwork_is_cached_and_uses_private_auth_header(self):
        requests = []
        image = b"\xff\xd8\xfftest-image"

        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=image, headers={"Content-Type": "image/jpeg"})

        source = PlexSource(
            self.settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        try:
            self.assertEqual(await source.artwork("123"), (image, "image/jpeg"))
            self.assertEqual(await source.artwork("123"), (image, "image/jpeg"))
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.path, "/library/metadata/123/thumb")
            self.assertEqual(requests[0].headers["X-Plex-Token"], "test-credential")
            self.assertNotIn("test-credential", str(requests[0].url))
        finally:
            await source.close()

    async def test_artwork_rejects_redirects_active_content_and_oversized_images(self):
        for response in [
            httpx.Response(302, headers={"Location": "https://other.example/image"}),
            httpx.Response(200, content=b"<svg/>", headers={"Content-Type": "image/svg+xml"}),
            httpx.Response(200, content=b"<html/>", headers={"Content-Type": "image/jpeg"}),
            httpx.Response(
                200, content=b"x" * (4 * 1024 * 1024 + 1), headers={"Content-Type": "image/jpeg"}
            ),
        ]:
            calls = []

            def handler(request):
                calls.append(request)
                return response

            source = PlexSource(
                self.settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
            )
            try:
                with self.assertRaises(PlexError):
                    await source.artwork("123")
                self.assertEqual(len(calls), 1)
                self.assertIsNone(source.cached_artwork("123"))
                with self.assertRaises(PlexError):
                    await source.artwork("../123")
                self.assertEqual(len(calls), 1)
            finally:
                await source.close()

    async def test_pagination_auth_and_track_metadata(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/library/sections":
                return httpx.Response(
                    200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "artist"}]}}
                )
            offset = int(request.url.params["X-Plex-Container-Start"])
            return httpx.Response(
                200, json={"MediaContainer": {"totalSize": 2, "Metadata": [item(str(offset + 1))]}}
            )

        source = PlexSource(
            self.settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        try:
            tracks = await source.catalog()
            self.assertEqual([t.id for t in tracks], ["1", "2"])
            for request in requests:
                self.assertEqual(request.headers["X-Plex-Token"], "test-credential")
                self.assertNotIn("test-credential", str(request.url))
            self.assertNotIn("part_key", tracks[0].public())
        finally:
            await source.close()

    async def test_access_error_is_safe(self):
        source = PlexSource(
            self.settings(),
            httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401))),
        )
        try:
            with self.assertRaisesRegex(PlexError, "^plex_access_denied$"):
                await source.catalog()
        finally:
            await source.close()

    async def test_media_url_cannot_redirect_token_to_other_host(self):
        source = PlexSource(self.settings())
        try:
            for path in [
                "//other.example/file",
                "https://other.example/file",
                "/library/../evil",
                "/\\evil",
            ]:
                with self.assertRaises(PlexError):
                    source._url(path)
        finally:
            await source.close()

    async def test_real_ffmpeg_conversion_seek_and_cache(self):
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(b"\x01\x00" * (44100 * 3))
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=data.getvalue())

        source = PlexSource(
            self.settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        track = PlexTrack("1", "Song", "a", "Album", "Artist", 1, 1, 3, "/library/parts/1/file")
        try:
            a, b = await asyncio.gather(source.prepare(track), source.prepare(track))
            self.assertEqual(a, b)
            self.assertEqual(len(calls), 1)
            audio = b"".join([chunk async for chunk in source.stream_track(track, offset=1)])
            self.assertGreater(len(audio), 20000)
            # Closing a reader midway must reap FFmpeg and its output pipe.
            partial = source.stream_track(track)
            self.assertTrue(await anext(partial))
            await asyncio.wait_for(partial.aclose(), timeout=2)
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "stream.mp3"
                path.write_bytes(audio + audio)
                result = subprocess.run(
                    ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
        finally:
            await source.close()

    async def test_invalid_audio_fails_and_is_not_cached(self):
        source = PlexSource(
            self.settings(),
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not audio"))
            ),
        )
        track = PlexTrack("1", "Song", "a", "Album", "Artist", 1, 1, 3, "/library/parts/1/file")
        try:
            with self.assertRaisesRegex(PlexError, "plex_transcode_failed"):
                await source.prepare(track)
            self.assertEqual(len(source._cache), 0)
            self.assertEqual(list(Path(source._temporary.name).iterdir()), [])
        finally:
            await source.close()
