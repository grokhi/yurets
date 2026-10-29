"""A real HTTP radio stream and real FFmpeg, using a local Plex API double."""

import asyncio
import io
import socket
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import httpx
import uvicorn

from src.main import create_app
from src.settings import Settings
from src.streaming.sources.plex import PlexSource


class LiveRadioTests(unittest.IsolatedAsyncioTestCase):
    async def test_plex_to_http_stream_decodes_across_track_transitions(self):
        wav_data = io.BytesIO()
        with wave.open(wav_data, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(b"\x01\x00" * 44100 * 8)
        media_requests = []

        def plex(request):
            self.assertEqual(request.headers["X-Plex-Token"], "test-only")
            if request.url.path == "/library/sections":
                return httpx.Response(
                    200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "artist"}]}}
                )
            if request.url.path == "/library/sections/1/all":
                tracks = [
                    {
                        "ratingKey": str(i),
                        "title": f"Tone {i}",
                        "parentRatingKey": "10",
                        "parentTitle": "Test Album",
                        "grandparentTitle": "Test Artist",
                        "parentIndex": 1,
                        "index": i,
                        "duration": 8000,
                        "Media": [{"Part": [{"key": f"/library/parts/{i}/file"}]}],
                    }
                    for i in (1, 2)
                ]
                return httpx.Response(
                    200, json={"MediaContainer": {"Metadata": tracks, "totalSize": 2}}
                )
            media_requests.append(request.url.path)
            return httpx.Response(200, content=wav_data.getvalue())

        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(
                _env_file=None,
                _env_prefix="YURETS_TEST_",
                data_dir=temp,
                plex_url="https://plex.test",
                plex_token="test-only",
                plex_library_id="1",
                timezone="UTC",
            )
            source = PlexSource(settings, httpx.AsyncClient(transport=httpx.MockTransport(plex)))
            app = create_app(settings, source=source)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", proxy_headers=False))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
                ) as client:
                    for _ in range(200):
                        if server.started and app.state.streamer.ready:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("The Plex pipeline did not become ready")
                    self.assertEqual((await client.get("/health")).status_code, 200)
                    self.assertEqual((await client.get("/")).status_code, 200)
                    before = app.state.streamer.diagnostics()["master"]["tracks_started"]
                    queue = (await client.get("/api/queue")).json()["tracks"]
                    observed = set()

                    async def observe():
                        while True:
                            observed.add((await client.get("/api/now-playing")).json()["track_id"])
                            await asyncio.sleep(0.25)

                    observer = asyncio.create_task(observe())
                    audio = bytearray()
                    try:
                        async with client.stream("GET", "/stream") as response:
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(response.headers["content-type"], "audio/mpeg")
                            async for chunk in response.aiter_bytes():
                                audio.extend(chunk)
                                if len(audio) >= 400_000:
                                    break
                    finally:
                        observer.cancel()
                        try:
                            await observer
                        except asyncio.CancelledError:
                            pass
                    self.assertIn(queue[0]["id"], observed)
                    self.assertGreater(
                        app.state.streamer.diagnostics()["master"]["tracks_started"], before
                    )
                    self.assertIsNone(app.state.streamer.diagnostics()["error"])
                    path = Path(temp) / "capture.mp3"
                    path.write_bytes(audio)
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertEqual(
                        set(media_requests), {"/library/parts/1/file", "/library/parts/2/file"}
                    )
                    self.assertEqual(
                        len(media_requests), 2, "prefetch should reuse the audio cache"
                    )
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=10)
                sock.close()
