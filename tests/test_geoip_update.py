import gzip
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from src.geoip_update import release_months, update_database
from src.services.analytics import Analytics
from src.services.database import Database
from src.settings import Settings


class GeoIPUpdateTests(unittest.TestCase):
    def test_release_fallback_handles_year_boundary(self):
        self.assertEqual(list(release_months(date(2026, 1, 3))), ["2026-01", "2025-12", "2025-11"])

    def test_download_validates_before_replacing_and_records_provenance(self):
        requests = []

        def handle(request):
            requests.append(str(request.url))
            if "2026-10" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, content=gzip.compress(b"candidate-database"))

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "country.mmdb"
            output.write_bytes(b"previous-database")

            def validate(path):
                self.assertEqual(path.read_bytes(), b"candidate-database")
                self.assertEqual(output.read_bytes(), b"previous-database")
                return "DBIP-Country-Lite"

            with (
                httpx.Client(transport=httpx.MockTransport(handle)) as client,
                patch("src.geoip_update.validate_database", side_effect=validate),
            ):
                record = update_database(output, today=date(2026, 10, 2), client=client)
            self.assertEqual(record["release"], "2026-09")
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(u.startswith("https://download.db-ip.com/free/") for u in requests))
            self.assertEqual(output.read_bytes(), b"candidate-database")
            self.assertTrue(output.with_suffix(".json").exists())

    def test_invalid_download_preserves_working_database(self):
        for payload in (b"not-gzip", gzip.compress(b"not-a-maxmind-database")):
            with self.subTest(payload_type=len(payload)), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "country.mmdb"
                output.write_bytes(b"previous-database")
                with httpx.Client(
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
                ) as client:
                    with self.assertRaises(Exception):
                        update_database(output, client=client)
                self.assertEqual(output.read_bytes(), b"previous-database")
                self.assertFalse(output.with_suffix(".json").exists())

    def test_permission_denial_does_not_try_older_releases(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(403)

        with (
            tempfile.TemporaryDirectory() as temp,
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                update_database(Path(temp) / "country.mmdb", client=client)
            self.assertEqual(len(calls), 1)

    def test_missing_database_keeps_non_geographic_statistics_available(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(
                _env_file=None,
                _env_prefix="YURETS_TEST_",
                data_dir=temp,
                geoip_database=str(Path(temp) / "missing.mmdb"),
            )
            database = Database(Path(temp) / "radio.sqlite3")
            analytics = Analytics(settings, database)
            try:
                self.assertFalse(analytics.summary(1)["geoip_available"])
                self.assertEqual(analytics.location("8.8.8.8"), ("8.8.8.8", None))
            finally:
                analytics.close()
                database.close()
