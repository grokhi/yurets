"""Optional Turnstile observation. Results never change audience eligibility."""

import asyncio
import time

import httpx


class Observation:
    action = "audience_observe"

    def __init__(self, settings, database, client=None):
        self.settings, self.database = settings, database
        self.enabled = bool(
            settings.turnstile_site_key and settings.turnstile_secret_key.get_secret_value()
        )
        self.client = client or httpx.AsyncClient(timeout=6, follow_redirects=False)
        self._slots = asyncio.Semaphore(4)

    async def close(self):
        await self.client.aclose()

    async def record(self, visit_id, page_token, response_token):
        if not self.enabled:
            return "disabled"
        with self.database.transaction() as db:
            # One provider request per page; duplicate callbacks cannot replay tokens.
            inserted = db.execute(
                "INSERT OR IGNORE INTO visit_observations(visit_id,status,at) VALUES (?, 'pending', ?)",
                (visit_id, time.time()),
            ).rowcount
            if not inserted:
                return db.execute(
                    "SELECT status FROM visit_observations WHERE visit_id=?", (visit_id,)
                ).fetchone()[0]
        status, reason = "unavailable", "browser_unavailable"
        if response_token:
            acquired = False
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=0.2)
                acquired = True
                result = await self.client.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    json={
                        "secret": self.settings.turnstile_secret_key.get_secret_value(),
                        "response": response_token,
                    },
                )
                result.raise_for_status()
                data = result.json()
                if data.get("success") is True:
                    if (
                        data.get("hostname") in self.settings.turnstile_hostnames
                        and data.get("action") == self.action
                        and data.get("cdata") == page_token.replace(".", "_")
                    ):
                        status, reason = "passed", "verified"
                    else:
                        status, reason = "not_passed", "context_mismatch"
                elif "invalid-input-response" in data.get("error-codes", []):
                    status, reason = "not_passed", "invalid_response"
                else:
                    status, reason = "unavailable", "verification_unavailable"
            except (httpx.HTTPError, ValueError, TypeError, AttributeError, asyncio.TimeoutError):
                status, reason = "unavailable", "service_unavailable"
            finally:
                if acquired:
                    self._slots.release()
        with self.database.transaction() as db:
            db.execute(
                "UPDATE visit_observations SET status=?,reason=? WHERE visit_id=?",
                (status, reason, visit_id),
            )
        return status
