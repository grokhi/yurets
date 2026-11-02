from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from src.services.traffic import known_bot


class Database:
    """One local database shared by the program planner and listener statistics."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.create_function("known_bot", 1, known_bot, deterministic=True)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS daily_plans (
                    namespace TEXT NOT NULL, day TEXT NOT NULL, entries TEXT NOT NULL,
                    PRIMARY KEY (namespace, day)
                );
                CREATE TABLE IF NOT EXISTS visitors (
                    id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    credited_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS visits (
                    id INTEGER PRIMARY KEY, visitor_id TEXT NOT NULL REFERENCES visitors(id),
                    at REAL NOT NULL, ip TEXT NOT NULL, country TEXT, user_agent TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS visits_at ON visits(at);
                CREATE TABLE IF NOT EXISTS visit_observations (
                    visit_id INTEGER PRIMARY KEY REFERENCES visits(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, reason TEXT, at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS listening_sessions (
                    id TEXT PRIMARY KEY, visitor_id TEXT NOT NULL REFERENCES visitors(id),
                    started_at REAL NOT NULL, last_seen REAL NOT NULL, ended_at REAL,
                    position REAL NOT NULL, seconds REAL NOT NULL DEFAULT 0,
                    ip TEXT NOT NULL, country TEXT
                );
                CREATE INDEX IF NOT EXISTS sessions_started ON listening_sessions(started_at);
                CREATE INDEX IF NOT EXISTS sessions_visitor ON listening_sessions(visitor_id);
            """)

        # Additive migrations keep old deployments and existing history readable.
        with self.transaction() as db:
            for table, column, declaration in (
                ("visits", "traffic_state", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("listening_sessions", "user_agent", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            db.executescript("""
                CREATE VIEW IF NOT EXISTS audience_visits AS
                    SELECT * FROM visits
                    WHERE traffic_state IN ('legacy', 'confirmed', 'navigation')
                    AND NOT known_bot(user_agent);
                CREATE VIEW IF NOT EXISTS audience_sessions AS
                    SELECT * FROM listening_sessions s
                    WHERE NOT known_bot(s.user_agent)
                    AND NOT (s.user_agent = ''
                        AND EXISTS (SELECT 1 FROM visits v WHERE v.visitor_id=s.visitor_id
                                    AND known_bot(v.user_agent))
                        AND NOT EXISTS (SELECT 1 FROM visits v WHERE v.visitor_id=s.visitor_id
                                        AND NOT known_bot(v.user_agent)));
            """)

    @contextmanager
    def transaction(self):
        with self._lock, self._connection:
            yield self._connection

    def close(self):
        with self._lock:
            self._connection.close()
