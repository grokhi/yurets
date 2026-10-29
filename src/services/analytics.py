from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
from pathlib import Path

import maxminddb

from src.services.database import Database
from src.settings import Settings


class Analytics:
    cookie_name = "yurets_listener"

    def __init__(self, settings: Settings, database: Database):
        self.settings, self.database = settings, database
        secret_path = Path(settings.data_dir) / "listener-cookie.key"
        try:
            descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(secrets.token_bytes(32))
        self._key = secret_path.read_bytes()
        if len(self._key) != 32:
            raise ValueError("Invalid listener cookie key")
        self._trusted = [ipaddress.ip_network(cidr) for cidr in settings.trusted_proxy_cidrs]
        self._geo = (
            maxminddb.open_database(settings.geoip_database)
            if settings.geoip_database and Path(settings.geoip_database).is_file()
            else None
        )
        self.purge()

    def close(self):
        if self._geo:
            self._geo.close()

    def signed(self, visitor: str):
        signature = hmac.new(self._key, visitor.encode(), hashlib.sha256).hexdigest()
        return visitor + "." + signature

    def identify(self, cookie: str | None):
        if cookie and re.fullmatch(r"[0-9a-f]{32}\.[0-9a-f]{64}", cookie):
            visitor = cookie.split(".")[0]
            if hmac.compare_digest(cookie, self.signed(visitor)):
                return visitor
        return secrets.token_hex(16)

    def location(self, peer: str, forwarded: str = ""):
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return "unknown", None
        # Walk right-to-left, accepting headers only from explicitly trusted hops.
        if any(address in network for network in self._trusted):
            for hop in reversed(forwarded.split(",")):
                if not any(address in network for network in self._trusted):
                    break
                try:
                    address = ipaddress.ip_address(hop.strip())
                except ValueError:
                    break
        country = None
        if self._geo and address.is_global:
            record = self._geo.get(str(address)) or {}
            country = record.get("country", {}).get("iso_code")
        return str(address), country

    def visit(
        self,
        visitor: str,
        ip: str,
        country: str | None,
        user_agent: str,
        *,
        page_view: bool = True,
        now: float | None = None,
    ):
        now = time.time() if now is None else now
        with self.database.transaction() as db:
            db.execute(
                """INSERT INTO visitors (id, first_seen, last_seen) VALUES (?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                (visitor, now, now),
            )
            if page_view:
                db.execute(
                    "INSERT INTO visits(visitor_id,at,ip,country,user_agent) VALUES (?,?,?,?,?)",
                    (visitor, now, ip, country, user_agent[:512]),
                )

    def listening(
        self,
        visitor: str,
        action: str,
        session_id: str | None,
        position: float,
        ip: str,
        country: str | None,
        *,
        now: float | None = None,
    ):
        now = time.time() if now is None else now
        self.visit(visitor, ip, country, "", page_view=False, now=now)
        with self.database.transaction() as db:
            if action == "start":
                session_id = secrets.token_hex(16)
                db.execute(
                    """INSERT INTO listening_sessions
                    (id,visitor_id,started_at,last_seen,position,ip,country) VALUES (?,?,?,?,?,?,?)""",
                    (session_id, visitor, now, now, position, ip, country),
                )
                return session_id
            session = db.execute(
                "SELECT * FROM listening_sessions WHERE id=? AND visitor_id=?",
                (session_id, visitor),
            ).fetchone()
            if session is None or session["ended_at"] is not None:
                return None
            credited_until = db.execute(
                "SELECT credited_until FROM visitors WHERE id=?", (visitor,)
            ).fetchone()[0]
            elapsed = max(0, now - session["last_seen"])
            progress = max(0, position - session["position"])
            # Delayed/background heartbeats do not fill hours of silence, and
            # overlapping tabs cannot multiply one listener's listening time.
            credit = min(elapsed, progress, 20, max(0, now - credited_until))
            db.execute(
                """UPDATE listening_sessions SET last_seen=?,position=?,seconds=seconds+?,
                          ended_at=? WHERE id=?""",
                (now, position, credit, now if action == "stop" else None, session_id),
            )
            db.execute(
                "UPDATE visitors SET credited_until=max(credited_until,?) WHERE id=?",
                (now, visitor),
            )
            return session_id

    def purge(self):
        cutoff = time.time() - self.settings.analytics_retention_days * 86400
        with self.database.transaction() as db:
            db.execute("DELETE FROM visits WHERE at < ?", (cutoff,))
            db.execute("DELETE FROM listening_sessions WHERE last_seen < ?", (cutoff,))
            db.execute(
                """DELETE FROM visitors WHERE last_seen < ?
                       AND id NOT IN (SELECT visitor_id FROM visits)
                       AND id NOT IN (SELECT visitor_id FROM listening_sessions)""",
                (cutoff,),
            )

    def summary(self, days: int, *, now: float | None = None):
        now = time.time() if now is None else now
        cutoff = now - days * 86400
        with self.database.transaction() as db:
            totals = dict(
                db.execute(
                    """SELECT COUNT(*) AS visits, COUNT(DISTINCT visitor_id) AS visitors
                                        FROM visits WHERE at >= ?""",
                    (cutoff,),
                ).fetchone()
            )
            totals.update(
                dict(
                    db.execute(
                        """SELECT COUNT(*) AS sessions,
                COALESCE(SUM(seconds), 0) AS listening_seconds FROM listening_sessions
                WHERE started_at >= ?""",
                        (cutoff,),
                    ).fetchone()
                )
            )
            totals["listening_now"] = db.execute(
                """SELECT COUNT(DISTINCT visitor_id)
                FROM listening_sessions WHERE ended_at IS NULL AND last_seen > ?""",
                (now - 20,),
            ).fetchone()[0]
            countries = [
                dict(row)
                for row in db.execute(
                    """SELECT COALESCE(country,'unknown') AS country,
                COUNT(*) AS visits, COUNT(DISTINCT visitor_id) AS visitors FROM visits
                WHERE at >= ? GROUP BY country ORDER BY visits DESC""",
                    (cutoff,),
                )
            ]
            listeners = [
                dict(row)
                for row in db.execute(
                    """SELECT s.visitor_id,
                COUNT(*) AS sessions, SUM(s.seconds) AS listening_seconds,
                MAX(s.last_seen) AS last_seen,
                (SELECT v.ip FROM visits v WHERE v.visitor_id=s.visitor_id ORDER BY v.at DESC LIMIT 1) AS ip,
                (SELECT v.country FROM visits v WHERE v.visitor_id=s.visitor_id ORDER BY v.at DESC LIMIT 1) AS country
                FROM listening_sessions s WHERE s.started_at >= ?
                GROUP BY s.visitor_id HAVING SUM(s.seconds)>0 ORDER BY listening_seconds DESC LIMIT 50""",
                    (cutoff,),
                )
            ]
            visits = [
                dict(row)
                for row in db.execute(
                    """SELECT visitor_id,at,ip,country,user_agent
                FROM visits WHERE at >= ? ORDER BY at DESC LIMIT 100""",
                    (cutoff,),
                )
            ]
            daily = [
                dict(row)
                for row in db.execute(
                    """SELECT date(at,'unixepoch') AS day,
                COUNT(*) AS visits, COUNT(DISTINCT visitor_id) AS visitors
                FROM visits WHERE at >= ? GROUP BY day ORDER BY day""",
                    (cutoff,),
                )
            ]
        return {
            "days": days,
            "totals": totals,
            "countries": countries,
            "listeners": listeners,
            "visits": visits,
            "daily": daily,
            "geoip_available": self._geo is not None,
            "geoip_provider": (
                "DB-IP Lite"
                if self._geo and self._geo.metadata().database_type.upper().startswith("DBIP")
                else None
            ),
            "retention_days": self.settings.analytics_retention_days,
        }
