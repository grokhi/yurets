import tempfile
import time
import unittest
from pathlib import Path

from src.services.analytics import Analytics
from src.services.database import Database
from src.settings import Settings


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None, _env_prefix="YURETS_TEST_", data_dir=self.temp.name, geoip_database=""
        )
        self.db = Database(Path(self.temp.name) / "radio.sqlite3")
        self.analytics = Analytics(self.settings, self.db)
        self.now = time.time()
        self.visitor = self.analytics.identify(None)
        self.analytics.visit(self.visitor, "8.8.8.8", "US", "<script>", now=self.now)

    def tearDown(self):
        self.analytics.close()
        self.db.close()
        self.temp.cleanup()

    def event(self, action, session=None, position=0, elapsed=0, visitor=None):
        return self.analytics.listening(
            visitor or self.visitor,
            action,
            session,
            position,
            "8.8.8.8",
            "US",
            now=self.now + elapsed,
        )

    def summary(self, elapsed=0):
        return self.analytics.summary(30, now=self.now + elapsed)

    def test_visits_do_not_count_as_listening(self):
        totals = self.summary()["totals"]
        self.assertEqual(totals["visits"], 1)
        self.assertEqual(totals["listening_seconds"], 0)

    def test_only_playback_progress_is_counted_and_stop_is_final(self):
        session = self.event("start")
        self.event("heartbeat", session, position=10, elapsed=10)
        self.event("stop", session, position=12, elapsed=15)
        self.event("heartbeat", session, position=50, elapsed=60)
        result = self.summary(60)
        self.assertEqual(result["totals"]["listening_seconds"], 12)
        self.assertEqual(result["totals"]["listening_now"], 0)
        self.assertEqual(result["listeners"][0]["visitor_id"], self.visitor)

    def test_stalled_and_delayed_heartbeats_do_not_invent_time(self):
        session = self.event("start")
        self.event("heartbeat", session, position=0, elapsed=15)
        self.assertEqual(self.summary(15)["totals"]["listening_seconds"], 0)
        self.event("heartbeat", session, position=1000, elapsed=1000)
        self.assertEqual(self.summary(1000)["totals"]["listening_seconds"], 20)

    def test_parallel_tabs_cannot_double_listening_time(self):
        a, b = self.event("start"), self.event("start")
        self.event("heartbeat", a, 10, 10)
        self.event("heartbeat", b, 10, 10)
        self.event("heartbeat", b, 20, 20)
        self.event("heartbeat", a, 20, 20)
        self.assertEqual(self.summary(20)["totals"]["listening_seconds"], 20)

    def test_cookie_is_signed_and_persists_across_restart(self):
        cookie = self.analytics.signed(self.visitor)
        self.assertEqual(self.analytics.identify(cookie), self.visitor)
        self.assertNotEqual(
            self.analytics.identify(cookie[:-1] + ("a" if cookie[-1] != "a" else "b")), self.visitor
        )
        restored = Analytics(self.settings, self.db)
        self.assertEqual(restored.identify(cookie), self.visitor)
        restored.close()

    def test_untrusted_forwarded_ip_is_ignored(self):
        self.assertEqual(self.analytics.location("192.0.2.3", "8.8.8.8"), ("192.0.2.3", None))
        configured = Analytics(
            self.settings.model_copy(update={"trusted_proxy_cidrs": ["127.0.0.0/8"]}), self.db
        )
        self.assertEqual(configured.location("127.0.0.1", "1.1.1.1, 8.8.8.8"), ("8.8.8.8", None))
        configured.close()

    def test_sessions_are_bound_to_visitor(self):
        session = self.event("start")
        other = self.analytics.identify(None)
        self.assertIsNone(self.event("heartbeat", session, 10, 10, visitor=other))
        self.assertEqual(self.summary(10)["totals"]["listening_seconds"], 0)

    def test_retention_removes_old_records(self):
        old = self.analytics.identify(None)
        self.analytics.visit(old, "1.1.1.1", None, "old", now=self.now - 100 * 86400)
        self.analytics.purge()
        self.assertEqual(self.analytics.summary(365)["totals"]["visits"], 1)
