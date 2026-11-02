"""Agent CLI, configuration and endpoint diagnostics without live writes."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sagasu.cli import main, parser
from sagasu.config import ConfigError, Settings
from sagasu.doctor import Check, diagnose


class AgentCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"
        self.path.write_text("")
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--env-file", str(self.path), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_no_credentials_required_for_agent_configuration(self):
        settings = Settings.load(self.path)
        settings.validate()
        self.assertEqual(settings.public()["mcp_confirmation"], "agent")
        code, text, _ = self.invoke("config", "show", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["mcp_confirmation"], "agent")

    def test_relative_paths_and_exported_browser_override(self):
        self.path.write_text(
            "SAGASU_BROWSER_PROFILE=private/browser\nSAGASU_PREFERENCES_PATH=private/contacts.sqlite3\nSAGASU_BROWSER_CHANNEL=chrome\n"
        )
        settings = Settings.load(self.path, browser="chromium")
        self.assertEqual(
            settings.profile, (self.path.parent / "private/browser").resolve()
        )
        self.assertEqual(
            settings.preferences,
            (self.path.parent / "private/contacts.sqlite3").resolve(),
        )
        self.assertEqual(settings.browser, "chromium")

    def test_password_autofill_requires_email_and_values_remain_literal(self):
        self.path.write_text("SMU_LOGIN_PASSWORD='private${VAR}#secret'\n")
        with self.assertRaises(ConfigError):
            Settings.load(self.path).validate()
        with self.path.open("a") as stream:
            stream.write("SMU_LOGIN_EMAIL=person@smu.edu.sg\n")
        settings = Settings.load(self.path)
        settings.validate()
        self.assertEqual(settings.values["SMU_LOGIN_PASSWORD"], "private${VAR}#secret")
        self.assertNotIn("private${VAR}", repr(settings))
        code, text, errors = self.invoke("config", "show", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(text)["password_autofill"])
        self.assertNotIn("private${VAR}", text + errors)

    def test_config_init_is_private_and_does_not_overwrite(self):
        self.path.unlink()
        self.assertEqual(self.invoke("config", "init")[0], 0)
        original = self.path.read_bytes()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.invoke("config", "init")[0], 2)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unsupported_commands_and_invalid_checks_fail_before_runtime(self):
        for args in (
            ("run",),
            ("mcp", "--telegram"),
            ("doctor", "--offline", "--session"),
            ("doctor", "--timeout", "0"),
        ):
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                main(list(args))
            self.assertEqual(error.exception.code, 2)

    def test_common_options_work_before_and_after_command(self):
        for args in (("--browser", "chrome", "mcp"), ("mcp", "--browser", "chrome")):
            self.assertEqual(parser().parse_args(args).browser, "chrome")


class AgentDoctorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = Path(self.directory.name) / ".env"
        env.write_text("")
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(env)

    async def test_offline_checks_local_runtime_without_network(self):
        with (
            patch(
                "sagasu.doctor.browser_check",
                AsyncMock(return_value=Check("browser", "pass", "Started")),
            ),
            patch("sagasu.doctor.probe", AsyncMock()) as network,
        ):
            report = await diagnose(self.settings, offline=True)
        self.assertTrue(report["ok"], report)
        network.assert_not_awaited()
        self.assertIn("dependency.mcp", [row["name"] for row in report["checks"]])

    async def test_fbs_outage_skips_session_without_opening_profile(self):
        with (
            patch(
                "sagasu.doctor.browser_check",
                AsyncMock(return_value=Check("browser", "pass", "Started")),
            ),
            patch(
                "sagasu.doctor.probe", AsyncMock(return_value=("timeout", None))
            ) as network,
            patch("sagasu.doctor.session_check", AsyncMock()) as session,
        ):
            report = await diagnose(self.settings, session=True)
        self.assertFalse(report["ok"])
        network.assert_awaited_once_with("fbs.intranet.smu.edu.sg", "/home", 10)
        session.assert_not_awaited()
        self.assertEqual(
            next(row for row in report["checks"] if row["name"] == "session")["status"],
            "skip",
        )
