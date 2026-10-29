import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.settings import Settings


class AdminPasswordSettingsTests(unittest.TestCase):
    def test_only_prefixed_environment_variable_is_accepted(self):
        for variables, expected in [
            ({"ADMIN_PASSWORD": "ignored-test"}, ""),
            ({"YURETS_ADMIN_PASSWORD": "configured-test"}, "configured-test"),
            (
                {"ADMIN_PASSWORD": "ignored-test", "YURETS_ADMIN_PASSWORD": "configured-test"},
                "configured-test",
            ),
            ({"ADMIN_PASSWORD": "ignored-test", "YURETS_ADMIN_PASSWORD": ""}, ""),
        ]:
            with self.subTest(names=list(variables)), patch.dict(os.environ, variables, clear=True):
                settings = Settings(_env_file=None)
                self.assertEqual(settings.admin_password.get_secret_value(), expected)
                if expected:
                    self.assertNotIn(expected, repr(settings))

    def test_process_variable_takes_precedence_over_dotenv(self):
        with tempfile.TemporaryDirectory() as temp:
            envfile = Path(temp) / ".env"
            envfile.write_text("YURETS_ADMIN_PASSWORD=file-test\n")
            with patch.dict(os.environ, {"YURETS_ADMIN_PASSWORD": "process-test"}, clear=True):
                settings = Settings(_env_file=envfile)
                self.assertEqual(settings.admin_password.get_secret_value(), "process-test")

    def test_dotenv_and_explicit_settings_are_supported(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            envfile = Path(temp) / ".env"
            envfile.write_text("YURETS_ADMIN_PASSWORD=file-test\n")
            self.assertEqual(
                Settings(_env_file=envfile).admin_password.get_secret_value(), "file-test"
            )
            settings = Settings(_env_file=envfile, admin_password="explicit-test")
            self.assertEqual(settings.admin_password.get_secret_value(), "explicit-test")

    def test_unprefixed_dotenv_variable_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            envfile = Path(temp) / ".env"
            envfile.write_text("ADMIN_PASSWORD=ignored-test\n")
            self.assertEqual(Settings(_env_file=envfile).admin_password.get_secret_value(), "")

    def test_missing_or_empty_password_stays_unconfigured(self):
        for variables in ({}, {"ADMIN_PASSWORD": "", "YURETS_ADMIN_PASSWORD": ""}):
            with patch.dict(os.environ, variables, clear=True):
                self.assertEqual(Settings(_env_file=None).admin_password.get_secret_value(), "")
