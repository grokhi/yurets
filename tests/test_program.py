import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.services.database import Database
from src.services.program import DailyProgram
from src.settings import Settings
from src.streaming.sources.plex import PlexError, PlexTrack


def track(id, album="a", number=1, disc=1, duration=1800):
    return PlexTrack(
        id,
        "Same title",
        album,
        "Album " + album,
        "Artist",
        disc,
        number,
        duration,
        f"/library/parts/{id}/file",
    )


class Catalog:
    def __init__(self, tracks):
        self.tracks, self.calls = tracks, 0

    async def catalog(self):
        self.calls += 1
        return self.tracks


class ProgramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.sqlite3")
        self.settings = Settings(
            _env_file=None,
            _env_prefix="YURETS_TEST_",
            plex_url="https://plex.example",
            plex_library_id="1",
            timezone="UTC",
        )
        self.source = Catalog([track("2", number=2), track("1"), track("3", "b")])
        self.now = datetime(2026, 10, 29, 12, 1, tzinfo=ZoneInfo("UTC"))
        self.program = DailyProgram(self.settings, self.source, self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def test_queue_matches_actual_future_tracks_and_does_not_consume(self):
        queue = await self.program.upcoming(self.now, 5)
        self.assertEqual(queue, await self.program.upcoming(self.now, 5))
        for entry in queue:
            actual = await self.program.current(
                datetime.fromtimestamp(entry.start + 1, ZoneInfo("UTC"))
            )
            self.assertEqual(actual, entry)
        self.assertEqual(await self.program.upcoming(self.now, 0), [])
        self.assertEqual(self.source.calls, 1)

    async def test_disc_order_and_same_title_tracks_are_not_deduplicated(self):
        self.settings.plex_album_ids = ["a", "b"]
        self.source.tracks.append(track("4", number=1, disc=2))
        program = DailyProgram(self.settings, self.source, self.db)
        entries = await program.entries(self.now)
        self.assertEqual([e.track.id for e in entries[:4]], ["1", "2", "4", "3"])

    async def test_plan_survives_restart_and_catalog_changes(self):
        first = await self.program.entries(self.now)
        other = Catalog([track("unrelated")])
        restored = DailyProgram(self.settings, other, self.db)
        self.assertEqual(first, await restored.entries(self.now))
        self.assertEqual(other.calls, 0)

    async def test_midnight_finishes_album_and_next_day_has_no_overlap(self):
        self.source.tracks = [track("1", duration=17000), track("2", number=2, duration=17000)]
        entries = await self.program.entries(self.now)
        last = entries[-1]
        tomorrow = self.now + timedelta(days=1)
        next_entries = await self.program.entries(tomorrow)
        fresh = [e for e in next_entries if e.block.startswith("2026-10-30")]
        self.assertGreater(last.end, datetime(2026, 10, 30, tzinfo=ZoneInfo("UTC")).timestamp())
        self.assertEqual(fresh[0].start, last.end)
        self.assertEqual(last.track.number, 2)
        self.assertTrue(all(a.end == b.start for a, b in zip(next_entries, next_entries[1:])))

    async def test_dst_day_uses_actual_midnight_boundaries(self):
        settings = self.settings.model_copy(update={"timezone": "America/New_York"})
        program = DailyProgram(settings, self.source, self.db)
        now = datetime(2026, 3, 8, 12, tzinfo=ZoneInfo("America/New_York"))
        entries = await program.entries(now)
        self.assertEqual(entries[-1].end - entries[0].start, 23 * 3600)

    async def test_missing_manual_album_fails_explicitly(self):
        self.settings.plex_album_ids = ["missing"]
        with self.assertRaisesRegex(PlexError, "plex_scheduled_album_missing"):
            await DailyProgram(self.settings, self.source, self.db).entries(self.now)

    async def test_local_day_clips_albums_and_preserves_shared_playback(self):
        self.source.tracks = [track("1", duration=17000), track("2", number=2, duration=17000)]
        for days in (2, 1, 0):
            await self.program.entries(self.now - timedelta(days=days))
        current = await self.program.current(self.now)
        queue = await self.program.upcoming(self.now, 5)
        for name in (
            "Asia/Tokyo",
            "America/Los_Angeles",
            "Asia/Kathmandu",
            "Pacific/Kiritimati",
            "Pacific/Honolulu",
        ):
            with self.subTest(zone=name):
                result = await self.program.schedule(self.now, timezone=name)
                begin, end = (datetime.fromisoformat(result[key]) for key in ("start_at", "end_at"))
                self.assertEqual(begin.astimezone(ZoneInfo(name)).hour, 0)
                self.assertEqual(begin.astimezone(ZoneInfo(name)).minute, 0)
                self.assertEqual(end.astimezone(ZoneInfo(name)).hour, 0)
                self.assertEqual(
                    result["date"], self.now.astimezone(ZoneInfo(name)).date().isoformat()
                )
                slots = result["slots"]
                self.assertEqual(slots[0]["start_at"], result["start_at"])
                self.assertEqual(slots[-1]["end_at"], result["end_at"])
                for slot in slots:
                    self.assertLess(
                        datetime.fromisoformat(slot["start_at"]),
                        datetime.fromisoformat(slot["end_at"]),
                    )
                    self.assertGreaterEqual(datetime.fromisoformat(slot["start_at"]), begin)
                    self.assertLessEqual(datetime.fromisoformat(slot["end_at"]), end)
                self.assertTrue(all(a["end_at"] == b["start_at"] for a, b in zip(slots, slots[1:])))
                self.assertEqual(await self.program.current(self.now), current)
                self.assertEqual(await self.program.upcoming(self.now, 5), queue)

    async def test_viewer_dst_day_covers_23_or_25_hours(self):
        for month, day, hours in ((3, 8, 23), (11, 1, 25)):
            now = datetime(2026, month, day, 12, tzinfo=ZoneInfo("America/New_York"))
            await self.program.entries(now - timedelta(days=1))
            result = await self.program.schedule(now, timezone="America/New_York")
            begin, end = (datetime.fromisoformat(result[key]) for key in ("start_at", "end_at"))
            self.assertEqual((end - begin).total_seconds(), hours * 3600)
            self.assertEqual(result["slots"][0]["start_at"], result["start_at"])
            self.assertEqual(result["slots"][-1]["end_at"], result["end_at"])

    async def test_viewing_missing_history_does_not_generate_a_different_past(self):
        original = await self.program.entries(self.now)
        result = await self.program.schedule(self.now, timezone="Asia/Tokyo")
        self.assertGreater(result["slots"][0]["start_at"], result["start_at"])
        self.assertIsNone(self.program._load(self.now.date() - timedelta(days=1)))
        self.assertEqual(await self.program.entries(self.now), original)
