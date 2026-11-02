import tempfile
import unittest
from pathlib import Path

import httpx

from src.services.analytics import Analytics
from src.services.database import Database
from src.services.observation import Observation
from src.settings import Settings


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_never_change_audience_and_are_verified_only_once(self):
        for outcome in ("passed", "not_passed", "unavailable", "mismatch"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                settings = Settings(
                    _env_file=None,
                    _env_prefix="YURETS_TEST_",
                    data_dir=temp,
                    turnstile_site_key="public-test",
                    turnstile_secret_key="secret-test",
                )
                db = Database(Path(temp) / "test.sqlite3")
                analytics = Analytics(settings, db)
                visit = analytics.visit(
                    analytics.identify(None), "8.8.8.8", "US", "Safari", traffic_state="confirmed"
                )
                token = analytics.visit_token(visit)
                calls = []

                def handler(request):
                    calls.append(request)
                    if outcome == "unavailable":
                        return httpx.Response(503)
                    if outcome == "not_passed":
                        return httpx.Response(
                            200, json={"success": False, "error-codes": ["invalid-input-response"]}
                        )
                    return httpx.Response(
                        200,
                        json={
                            "success": True,
                            "hostname": "yurets.you",
                            "action": "wrong" if outcome == "mismatch" else "audience_observe",
                            "cdata": token.replace(".", "_"),
                        },
                    )

                service = Observation(
                    settings, db, httpx.AsyncClient(transport=httpx.MockTransport(handler))
                )
                try:
                    baseline = analytics.summary(30)["totals"]
                    result = await service.record(visit, token, "widget-response")
                    self.assertEqual(result, "not_passed" if outcome == "mismatch" else outcome)
                    self.assertEqual(await service.record(visit, token, "widget-response"), result)
                    self.assertEqual(len(calls), 1)
                    summary = analytics.summary(30)
                    self.assertEqual(summary["totals"], baseline)
                    self.assertEqual(summary["observation"]["counts"], {result: 1})
                    self.assertNotIn("secret-test", str(summary))
                    self.assertNotIn("widget-response", str(summary))
                    self.assertIsNone(analytics.verified_visit_id("1." + "0" * 64))
                    self.assertEqual(analytics.verified_visit_id(token), int(visit))
                finally:
                    await service.close()
                    analytics.close()
                    db.close()

    async def test_disabled_and_missing_browser_result_do_not_call_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(_env_file=None, _env_prefix="YURETS_TEST_", data_dir=temp)
            db = Database(Path(temp) / "test.sqlite3")
            analytics = Analytics(settings, db)
            visit = analytics.visit(analytics.identify(None), "8.8.8.8", "US", "Safari")

            def unexpected(_):
                raise AssertionError("Must not contact provider")

            service = Observation(
                settings, db, httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
            )
            try:
                self.assertEqual(
                    await service.record(visit, analytics.visit_token(visit), None), "disabled"
                )
                service.enabled = True
                self.assertEqual(
                    await service.record(visit, analytics.visit_token(visit), None), "unavailable"
                )
                self.assertEqual(analytics.summary(30)["totals"]["visits"], 1)
            finally:
                await service.close()
                analytics.close()
                db.close()
