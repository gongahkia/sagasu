"""Local configuration diagnostics never open a browser or change private data."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sagasu.cli import main
from sagasu.config import ConfigError, Settings
from sagasu.diagnostics import configuration_status
from sagasu.doctor import diagnose


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = self.root / ".env"
        self.base = "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\nSAGASU_JOURNAL_PATH=actions.sqlite3\n"
        self.env.write_text(self.base)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_all_invalid_settings_are_reported_without_echoing_values(self):
        secret = "PRIVATE_MISTYPED_VALUE"
        self.env.write_text(
            self.base
            + f"SAGASU_BROWSER_CHANNEL={secret}\nSAGASU_AUTH_TIMEOUT_MS={secret}\nSAGASU_READ_BACKEND={secret}\nSAGASU_MCP_CONFIRMATION={secret}\nSMU_LOGIN_PASSWORD='{secret}'\n"
        )
        settings = Settings.load(self.env, diagnostic=True)
        report = configuration_status(settings)
        self.assertFalse(report["operational"])
        names = {
            check["name"] for check in report["checks"] if check["status"] == "fail"
        }
        self.assertTrue(
            {
                "config.browser",
                "config.auth_timeout",
                "config.read_backend",
                "config.confirmation",
                "config.login_email",
            }
            <= names
        )
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, json.dumps(settings.public()))
        with self.assertRaises(ConfigError):
            settings.validate()
        self.assertFalse((self.root / "browser").exists())
        self.assertFalse((self.root / "personal.sqlite3").exists())

    def test_missing_credentials_do_not_block_saved_session_operations(self):
        self.env.write_text(self.base + "SAGASU_HEADLESS=true\n")
        report = configuration_status(Settings.load(self.env, diagnostic=True))
        self.assertTrue(report["operational"])
        self.assertTrue(report["capabilities"]["fbs_operations"])
        self.assertFalse(report["capabilities"]["automatic_login"])
        self.assertEqual(
            report["authentication"]["session_valid"], "unknown_until_checked"
        )
        names = {
            check["name"] for check in report["checks"] if check["status"] == "warn"
        }
        self.assertTrue({"authentication.email", "authentication.password"} <= names)

    def test_password_without_email_only_blocks_autofill(self):
        self.env.write_text(self.base + "SMU_LOGIN_PASSWORD=PRIVATE_PASSWORD\n")
        settings = Settings.load(self.env)
        with self.assertRaisesRegex(ConfigError, "SMU_LOGIN_EMAIL"):
            settings.validate()
        report = configuration_status(settings)
        self.assertFalse(report["ok"])
        self.assertTrue(report["operational"])
        self.assertFalse(report["capabilities"]["automatic_login"])
        self.assertNotIn("PRIVATE_PASSWORD", json.dumps(report))

    def test_strict_load_still_rejects_invalid_timeout(self):
        self.env.write_text(self.base + "SAGASU_AUTH_TIMEOUT_MS=invalid\n")
        with self.assertRaises(ConfigError):
            Settings.load(self.env)

    def test_invalid_fallback_and_corrected_config_have_different_host_identities(self):
        from sagasu.host import fingerprint

        self.env.write_text(self.base + "SAGASU_AUTH_TIMEOUT_MS=invalid\n")
        invalid = Settings.load(self.env, diagnostic=True)
        self.env.write_text(self.base + "SAGASU_AUTH_TIMEOUT_MS=600000\n")
        corrected = Settings.load(self.env, diagnostic=True)
        self.assertEqual(invalid.public(), corrected.public())
        self.assertNotEqual(fingerprint(invalid), fingerprint(corrected))

    def test_env_directory_and_invalid_path_leave_diagnostics_available(self):
        settings = Settings.load(self.root, diagnostic=True)
        self.assertFalse(configuration_status(settings)["operational"])
        settings = Settings.load("invalid\0path", diagnostic=True)
        self.assertFalse(configuration_status(settings)["operational"])

    def test_inaccessible_environment_file_is_diagnosable_without_echoing_errors(self):
        original_stat = Path.stat
        target = self.env.resolve()

        def inaccessible(path, *args, **kwargs):
            if path == target:
                raise PermissionError("PRIVATE_FILESYSTEM_ERROR")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", inaccessible):
            settings = Settings.load(self.env, diagnostic=True)
            report = configuration_status(settings)
            self.assertFalse(report["operational"])
            self.assertFalse(settings.public()["env_file_exists"])
            self.assertNotIn("PRIVATE_FILESYSTEM_ERROR", json.dumps(report))

    def test_invalid_env_path_does_not_load_another_files_credentials(self):
        self.env.write_text(
            self.base + "SMU_LOGIN_PASSWORD=PRIVATE_FALLBACK_PASSWORD\n"
        )
        with patch("sagasu.config.DEFAULT_ENV", self.env):
            settings = Settings.load("invalid\0path", diagnostic=True)
        self.assertNotIn("SMU_LOGIN_PASSWORD", settings.values)
        self.assertFalse(configuration_status(settings)["operational"])

    def test_wrong_storage_type_is_reported_without_resetting_data(self):
        profile = self.root / "browser"
        profile.write_text("PRIVATE_DATA")
        report = configuration_status(Settings.load(self.env))
        self.assertFalse(report["operational"])
        self.assertEqual(profile.read_text(), "PRIVATE_DATA")
        self.assertNotIn("PRIVATE_DATA", json.dumps(report))

    def test_doctor_returns_same_local_configuration_checks(self):
        self.env.write_text(self.base + "SAGASU_AUTH_TIMEOUT_MS=invalid\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["doctor", "--env-file", str(self.env), "--offline", "--json"]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        expected = configuration_status(Settings.load(self.env, diagnostic=True))[
            "checks"
        ]
        self.assertEqual(report["checks"][: len(expected)], expected)


class InvalidConfigurationDoctorTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_configuration_never_starts_browser_or_network_checks(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            env = Path(directory) / ".env"
            env.write_text("SAGASU_READ_BACKEND=invalid\n")
            settings = Settings.load(env, diagnostic=True)
            with (
                patch("sagasu.doctor.browser_check", AsyncMock()) as browser,
                patch("sagasu.doctor.probe", AsyncMock()) as network,
                patch("sagasu.doctor.session_check", AsyncMock()) as session,
            ):
                report = await diagnose(settings, session=True)
            self.assertFalse(report["ok"])
            browser.assert_not_awaited()
            network.assert_not_awaited()
            session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
