import tempfile
import time
import unittest
from pathlib import Path

from src.services.analytics import Analytics
from src.services.database import Database
from src.settings import Settings
from src.services.traffic import known_bot


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

    def test_pending_visits_are_confirmed_once_and_bad_tokens_rejected(self):
        visit = self.analytics.visit(
            self.visitor, "8.8.8.8", "US", "", traffic_state="pending", now=self.now
        )
        self.assertEqual(self.summary()["totals"]["visits"], 1)
        self.assertFalse(self.analytics.confirm_visit(visit + "." + "0" * 64, ""))
        token = self.analytics.visit_token(visit)
        self.assertTrue(self.analytics.confirm_visit(token, "", now=self.now))
        self.assertTrue(self.analytics.confirm_visit(token, "", now=self.now))
        self.assertEqual(self.summary()["totals"]["visits"], 2)

    def test_historical_bots_are_filtered_without_deleting_history(self):
        bot = self.analytics.identify(None)
        self.analytics.visit(bot, "1.1.1.1", "AU", "Mozilla/5.0 Googlebot/2.1", now=self.now)
        # Simulate a legacy record whose bot status was not classified on insertion.
        with self.db.transaction() as db:
            db.execute("UPDATE visits SET traffic_state='legacy' WHERE visitor_id=?", (bot,))
        session = self.event("start", visitor=bot)
        self.event("stop", session, position=10, elapsed=10, visitor=bot)
        result = self.summary(10)
        self.assertEqual(result["totals"]["visits"], 1)
        self.assertEqual(result["totals"]["sessions"], 0)
        self.assertEqual(result["traffic_filter"]["bots"], 1)
        self.assertEqual(result["traffic_filter"]["legacy"], 1)
        self.assertEqual(result["countries"], [{"country": "US", "visits": 1, "visitors": 1}])
        with self.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 2)
        restored = Database(Path(self.temp.name) / "radio.sqlite3")
        restored.close()

    def test_scanners_observed_on_site_are_excluded_without_blocking_real_browsers(self):
        for agent in [
            "Mozilla/5.0 (compatible; Infrawatch/1.0)",
            "Mozilla/5.0 (compatible; CensysInspect/1.1)",
            "Mozilla/5.0 (compatible; CyberConvoyScout/1.0)",
            "Mozilla/5.0 zgrab/0.x",
            "Googlebot/2.1",
            "curl/8.0",
        ]:
            self.assertTrue(known_bot(agent), agent)
        for agent in [
            "",
            "Mozilla/5.0 (Linux; Android 10; CUBOT) Chrome/156.0 Mobile Safari/537.36",
            "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 Safari/604.1",
            "Mozilla/5.0 (Macintosh) Claude/4.29.0 Chrome/152.0 Safari/537.36",
        ]:
            self.assertFalse(known_bot(agent), agent)

    def test_existing_database_can_be_migrated_without_losing_history(self):
        import sqlite3

        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
                CREATE TABLE visitors (id TEXT PRIMARY KEY, first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL, credited_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE visits (id INTEGER PRIMARY KEY, visitor_id TEXT NOT NULL,
                    at REAL NOT NULL, ip TEXT NOT NULL, country TEXT, user_agent TEXT NOT NULL);
                CREATE TABLE listening_sessions (id TEXT PRIMARY KEY, visitor_id TEXT NOT NULL,
                    started_at REAL NOT NULL, last_seen REAL NOT NULL, ended_at REAL,
                    position REAL NOT NULL, seconds REAL NOT NULL DEFAULT 0, ip TEXT NOT NULL, country TEXT);
                INSERT INTO visitors VALUES ('old', 1, 1, 0);
                INSERT INTO visits VALUES (1, 'old', 1, '8.8.8.8', 'US', 'Safari');
            """)
        migrated = Database(legacy_path)
        try:
            with migrated.transaction() as db:
                self.assertEqual(
                    db.execute("SELECT traffic_state FROM visits").fetchone()[0], "legacy"
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM audience_visits").fetchone()[0], 1
                )
        finally:
            migrated.close()

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
