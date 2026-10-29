import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.plex_link import link


class PlexLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_reuses_approved_login_and_removes_pending_secret(self):
        def handle(request):
            self.assertNotIn("/pins", request.url.path)
            if request.url.path == "/api/v2/resources":
                self.assertEqual(request.headers["X-Plex-Token"], "pending-test-token")
                return httpx.Response(
                    200,
                    json=[
                        {
                            "clientIdentifier": "server-id",
                            "provides": "server",
                            "connections": [{"uri": "https://media.example:32400"}],
                        }
                    ],
                )
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [
                            {"key": "7", "title": "Music", "type": "artist"},
                        ]
                    }
                },
            )

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / ".env.plex"
            pending = Path(str(output) + ".pending")
            pending.write_text(
                json.dumps(
                    {
                        "server_id": "server-id",
                        "client_id": "client-id",
                        "token": "pending-test-token",
                    }
                )
            )
            args = SimpleNamespace(server_id="server-id", library_id=None, output=str(output))
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            log = io.StringIO()
            with (
                patch("src.plex_link.httpx.AsyncClient", return_value=client),
                redirect_stdout(log),
            ):
                await link(args)
            self.assertFalse(pending.exists())
            self.assertTrue(output.exists())
            self.assertNotIn("pending-test-token", log.getvalue())

    async def test_browser_confirmation_saves_private_config_without_printing_token(self):
        def handle(request):
            if request.url.path == "/api/v2/pins":
                return httpx.Response(200, json={"id": 1, "code": "test-pin", "expiresIn": 60})
            if request.url.path == "/api/v2/pins/1":
                return httpx.Response(200, json={"authToken": "account-test-token"})
            if request.url.path == "/api/v2/resources":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "clientIdentifier": "server-id",
                            "provides": "server",
                            "accessToken": "server-test-token",
                            "connections": [{"uri": "https://media.example:32400"}],
                        }
                    ],
                )
            self.assertEqual(request.headers["X-Plex-Token"], "server-test-token")
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [
                            {"key": "7", "title": "Music", "type": "artist"},
                        ]
                    }
                },
            )

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / ".env.plex"
            args = SimpleNamespace(server_id="server-id", library_id=None, output=str(output))
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            log = io.StringIO()
            with (
                patch("src.plex_link.httpx.AsyncClient", return_value=client),
                patch("src.plex_link.asyncio.sleep", new_callable=AsyncMock),
                redirect_stdout(log),
            ):
                await link(args)
            self.assertIn("https://app.plex.tv/auth#?", log.getvalue())
            self.assertNotIn("account-test-token", log.getvalue())
            self.assertNotIn("server-test-token", log.getvalue())
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(json.loads(values["YURETS_PLEX_LIBRARY_ID"]), "7")
            self.assertEqual(json.loads(values["YURETS_PLEX_TOKEN"]), "server-test-token")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                await link(args)
