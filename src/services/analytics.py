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
from src.services.traffic import known_bot


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
        traffic_state: str = "legacy",
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
                cursor = db.execute(
                    "INSERT INTO visits(visitor_id,at,ip,country,user_agent,traffic_state) VALUES (?,?,?,?,?,?)",
                    (
                        visitor,
                        now,
                        ip,
                        country,
                        user_agent[:512],
                        "bot" if known_bot(user_agent) else traffic_state,
                    ),
                )
                return str(cursor.lastrowid)

    def visit_token(self, visit_id: str) -> str:
        signature = hmac.new(self._key, f"visit:{visit_id}".encode(), hashlib.sha256).hexdigest()
        return f"{visit_id}.{signature}"

    def verified_visit_id(self, token: str):
        if not re.fullmatch(r"[0-9]{1,20}\.[0-9a-f]{64}", token):
            return None
        visit_id = token.split(".")[0]
        if not hmac.compare_digest(token, self.visit_token(visit_id)):
            return None
        with self.database.transaction() as db:
            return (
                int(visit_id)
                if db.execute("SELECT 1 FROM visits WHERE id=?", (visit_id,)).fetchone()
                else None
            )

    def confirm_visit(self, token: str, user_agent: str, *, now: float | None = None):
        if known_bot(user_agent) or not re.fullmatch(r"[0-9]{1,20}\.[0-9a-f]{64}", token):
            return False
        visit_id = token.split(".")[0]
        if not hmac.compare_digest(token, self.visit_token(visit_id)):
            return False
        with self.database.transaction() as db:
            visit = db.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
            if not visit or known_bot(visit["user_agent"]) or visit["traffic_state"] == "bot":
                return False
            if visit["traffic_state"] == "pending":
                at = time.time() if now is None else now
                db.execute(
                    "UPDATE visits SET traffic_state='confirmed', at=? WHERE id=?", (at, visit_id)
                )
            return True

    def listening(
        self,
        visitor: str,
        action: str,
        session_id: str | None,
        position: float,
        ip: str,
        country: str | None,
        *,
        user_agent: str = "",
        now: float | None = None,
    ):
        if known_bot(user_agent):
            return None
        now = time.time() if now is None else now
        self.visit(visitor, ip, country, "", page_view=False, now=now)
        with self.database.transaction() as db:
            if action == "start":
                session_id = secrets.token_hex(16)
                db.execute(
                    """INSERT INTO listening_sessions
                    (id,visitor_id,started_at,last_seen,position,ip,country,user_agent) VALUES (?,?,?,?,?,?,?,?)""",
                    (session_id, visitor, now, now, position, ip, country, user_agent[:512]),
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
                                        FROM audience_visits WHERE at >= ?""",
                    (cutoff,),
                ).fetchone()
            )
            totals.update(
                dict(
                    db.execute(
                        """SELECT COUNT(*) AS sessions,
                COALESCE(SUM(seconds), 0) AS listening_seconds FROM audience_sessions
                WHERE started_at >= ?""",
                        (cutoff,),
                    ).fetchone()
                )
            )
            totals["listening_now"] = db.execute(
                """SELECT COUNT(DISTINCT visitor_id)
                FROM audience_sessions WHERE ended_at IS NULL AND last_seen > ?""",
                (now - 20,),
            ).fetchone()[0]
            countries = [
                dict(row)
                for row in db.execute(
                    """SELECT COALESCE(country,'unknown') AS country,
                COUNT(*) AS visits, COUNT(DISTINCT visitor_id) AS visitors FROM audience_visits
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
                (SELECT v.ip FROM audience_visits v WHERE v.visitor_id=s.visitor_id ORDER BY v.at DESC LIMIT 1) AS ip,
                (SELECT v.country FROM audience_visits v WHERE v.visitor_id=s.visitor_id ORDER BY v.at DESC LIMIT 1) AS country
                FROM audience_sessions s WHERE s.started_at >= ?
                GROUP BY s.visitor_id HAVING SUM(s.seconds)>0 ORDER BY listening_seconds DESC LIMIT 50""",
                    (cutoff,),
                )
            ]
            visits = [
                dict(row)
                for row in db.execute(
                    """SELECT v.visitor_id,v.at,v.ip,v.country,v.user_agent,
                    COALESCE(o.status,'not_checked') AS observation_status
                FROM audience_visits v LEFT JOIN visit_observations o ON o.visit_id=v.id
                WHERE v.at >= ? ORDER BY v.at DESC LIMIT 100""",
                    (cutoff,),
                )
            ]
            daily = [
                dict(row)
                for row in db.execute(
                    """SELECT date(at,'unixepoch') AS day,
                COUNT(*) AS visits, COUNT(DISTINCT visitor_id) AS visitors
                FROM audience_visits WHERE at >= ? GROUP BY day ORDER BY day""",
                    (cutoff,),
                )
            ]
            traffic = dict(
                db.execute(
                    """SELECT
                SUM(CASE WHEN known_bot(user_agent) OR traffic_state='bot' THEN 1 ELSE 0 END) AS bots,
                SUM(CASE WHEN traffic_state='pending' AND NOT known_bot(user_agent) THEN 1 ELSE 0 END) AS unconfirmed,
                SUM(CASE WHEN traffic_state='legacy' AND NOT known_bot(user_agent) THEN 1 ELSE 0 END) AS legacy
                FROM visits WHERE at >= ?""",
                    (cutoff,),
                ).fetchone()
            )
            observation_counts = {
                row["status"]: row["count"]
                for row in db.execute(
                    """SELECT COALESCE(o.status,'not_checked') AS status, COUNT(*) AS count
                FROM visits v LEFT JOIN visit_observations o ON o.visit_id=v.id
                WHERE v.at >= ? GROUP BY COALESCE(o.status,'not_checked')""",
                    (cutoff,),
                )
            }
        return {
            "observation": {
                "mode": "observe_only",
                "enabled": bool(
                    self.settings.turnstile_site_key
                    and self.settings.turnstile_secret_key.get_secret_value()
                ),
                "counts": observation_counts,
            },
            "traffic_filter": {k: v or 0 for k, v in traffic.items()},
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
