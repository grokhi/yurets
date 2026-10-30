import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.main import create_app
from src.services.database import Database
from src.settings import Settings
from src.streaming.streamer import Streamer
from test_program import track


class FakePlex:
    def configuration_error(self):
        return None

    async def catalog(self):
        return [track("1"), track("2", number=2)]

    async def prepare(self, track):
        return Path("/unused")

    async def stream_track(self, track, offset=0):
        while True:
            yield b"audio" * 100
            await asyncio.sleep(0.01)

    async def close(self):
        pass


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_public_stats_cookie_queue_and_no_telegram_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy_env = Path(temp) / ".env"
            legacy_env.write_text("YURETS_ADMIN_PASSWORD=legacy-password\n")
            settings = Settings(
                _env_file=legacy_env,
                _env_prefix="YURETS_TEST_",
                data_dir=temp,
                plex_url="https://plex.example",
                plex_library_id="1",
                timezone="UTC",
            )
            app = create_app(settings, source=FakePlex())
            with patch(
                "src.streaming.sources.telegram.TelegramSession.startup",
                side_effect=AssertionError("Telegram must stay off"),
            ):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="https://test"
                    ) as client:
                        await asyncio.sleep(0.05)
                        home = await client.get("/")
                        self.assertEqual(home.status_code, 200)
                        self.assertIn("HttpOnly", home.headers["set-cookie"])
                        self.assertIn("Secure", home.headers["set-cookie"])
                        self.assertTrue(client.cookies.get("yurets_listener"))
                        listener_cookie = client.cookies.get("yurets_listener")
                        self.assertEqual((await client.get("/")).status_code, 200)
                        self.assertEqual(client.cookies.get("yurets_listener"), listener_cookie)
                        self.assertEqual(home.headers["cache-control"], "no-store")
                        self.assertEqual((await client.get("/health")).status_code, 200)
                        first = (await client.get("/api/queue")).json()
                        self.assertEqual(first, (await client.get("/api/queue")).json())
                        self.assertEqual(
                            (await client.get("/api/queue?count=0")).json()["tracks"], []
                        )
                        self.assertNotIn("part_key", str(first))
                        self.assertEqual((await client.get("/api/analytics")).status_code, 200)
                        self.assertEqual((await client.get("/stats")).status_code, 200)
                        self.assertEqual((await client.get("/static/stats.html")).status_code, 404)
                        stats = (await client.get("/api/analytics")).json()
                        self.assertNotIn("legacy-password", str(stats))
                        self.assertNotIn("www-authenticate", (await client.get("/stats")).headers)
                        self.assertEqual(stats["totals"]["visits"], 2)
                        self.assertEqual(stats["totals"]["visitors"], 1)
                        self.assertEqual(stats["totals"]["listening_seconds"], 0)
                        self.assertEqual(
                            (
                                await client.get("/api/analytics", auth=("radio", "wrong"))
                            ).status_code,
                            200,
                        )
                        start = await client.post(
                            "/api/listening", json={"action": "start", "position": 0}
                        )
                        self.assertTrue(start.json()["session_id"])
                        invalid = await client.post(
                            "/api/listening", json={"action": "heartbeat", "position": -1}
                        )
                        self.assertEqual(invalid.status_code, 422)

    async def test_missing_plex_has_no_local_or_telegram_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(
                Settings(
                    _env_file=None,
                    _env_prefix="YURETS_TEST_",
                    data_dir=temp,
                )
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    self.assertEqual((await client.get("/")).status_code, 200)
                    health = await client.get("/health")
                    self.assertEqual(health.status_code, 503)
                    self.assertEqual(health.json()["error"], "plex_not_configured")
                    self.assertEqual((await client.get("/stream")).status_code, 503)
                    self.assertEqual((await client.get("/api/schedule")).status_code, 503)
                    self.assertEqual((await client.get("/api/analytics")).status_code, 200)
                    self.assertEqual((await client.get("/stats")).status_code, 200)

    async def test_slow_subscriber_is_closed_without_corrupting_other_streams(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "db.sqlite3")
            streamer = Streamer(
                Settings(_env_file=None, _env_prefix="YURETS_TEST_", subscriber_queue_chunks=1),
                database,
                FakePlex(),
            )
            fast, slow = streamer.subscribe(), streamer.subscribe()
            a, b = asyncio.create_task(anext(fast)), asyncio.create_task(anext(slow))
            await asyncio.sleep(0)
            streamer._broadcast(b"one")
            self.assertEqual(await a, b"one")
            self.assertEqual(await b, b"one")
            streamer._broadcast(b"two")
            self.assertEqual(await anext(fast), b"two")
            streamer._broadcast(b"three")
            self.assertEqual(await anext(fast), b"three")
            with self.assertRaises(StopAsyncIteration):
                await anext(slow)
            self.assertEqual(streamer.diagnostics()["subscribers"]["dropped_total"], 1)
            await fast.aclose()
            await streamer.shutdown()
            database.close()
