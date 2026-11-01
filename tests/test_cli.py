import asyncio
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import InvalidToken

from bot import __version__
from bot._vendor.rattles import DOTS, LINE
from bot.cli import main, parser
from bot.config import ConfigError, Settings
from bot.console import Redact, Reporter
from bot.doctor import Check, diagnose
from bot.runtime import AlreadyRunning, InstanceLock

TOKEN = "123456789:abcdefghijklmnopqrstuvwxyz_123456789"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"
        self.path.write_text(
            f"BOT_TOKEN={TOKEN}\nTELEGRAM_OWNER_ID=42\nSAGASU_BROWSER_CHANNEL=chromium\n"
        )
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--env-file", str(self.path), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_bare_cli_help_needs_no_credentials(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        self.assertIn("doctor", output.getvalue())

    def test_version(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                main(["--version"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn(__version__, output.getvalue())

    def test_options_work_before_and_after_subcommands(self):
        for arguments in (
            ["--browser", "chrome", "doctor"],
            ["doctor", "--browser", "chrome"],
            ["--env-file", "x", "config", "show"],
            ["config", "show", "--env-file", "x"],
        ):
            parsed = parser().parse_args(arguments)
            if "chrome" in arguments:
                self.assertEqual(parsed.browser, "chrome")
            else:
                self.assertEqual(str(parsed.env_file), "x")

    def test_environment_overrides_file_and_cli_browser_overrides_both(self):
        with patch.dict(
            os.environ,
            {"BOT_TOKEN": "999:" + "x" * 30, "SAGASU_BROWSER_CHANNEL": "chrome"},
        ):
            settings = Settings.load(self.path, browser="chromium")
        self.assertTrue(settings.token.startswith("999:"))
        self.assertEqual(settings.browser, "chromium")
        self.assertNotIn(settings.token, repr(settings))

    def test_relative_paths_resolve_against_explicit_environment_file(self):
        self.path.write_text(
            "SAGASU_BROWSER_PROFILE=private/browser\nSAGASU_STATE_PATH=private/state.pickle\n"
        )
        settings = Settings.load(self.path)
        self.assertEqual(settings.profile, self.path.parent / "private/browser")
        self.assertEqual(
            settings.persistence, self.path.parent / "private/state.pickle"
        )

    def test_config_show_redacts_token(self):
        code, stdout, stderr = self.invoke("config", "show", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, stdout + stderr)
        self.assertEqual(json.loads(stdout)["bot_token"], "configured (redacted)")

    def test_config_init_is_private_and_never_overwrites(self):
        self.path.unlink()
        code, _, _ = self.invoke("config", "init")
        self.assertEqual(code, 0)
        original = self.path.read_bytes()
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        code, _, stderr = self.invoke("config", "init")
        self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIn("already exists", stderr)

    def test_placeholder_token_fails_before_telegram_startup(self):
        self.path.write_text("BOT_TOKEN=replace-with-your-telegram-bot-token\n")
        with patch("bot.cli.run") as run:
            code, _, stderr = self.invoke("run")
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("@BotFather", stderr)
        self.assertNotIn("replace-with-your-telegram-bot-token", stderr)

    def test_server_rejected_token_has_no_traceback_or_token(self):
        with patch("bot.cli.run", side_effect=InvalidToken(TOKEN)):
            code, _, stderr = self.invoke("run")
        self.assertEqual(code, 1)
        self.assertIn("Telegram rejected", stderr)
        self.assertNotIn(TOKEN, stderr)
        self.assertNotIn("Traceback", stderr)

    def test_invalid_owner_fails_before_run(self):
        self.path.write_text(f"BOT_TOKEN={TOKEN}\nTELEGRAM_OWNER_ID=@username\n")
        with patch("bot.cli.run") as run:
            code, _, stderr = self.invoke("run")
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("numeric", stderr)

    def test_keyboard_interrupt_has_predictable_exit(self):
        with patch("bot.cli.run", side_effect=KeyboardInterrupt()):
            code, _, stderr = self.invoke("run")
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", stderr)

    def test_doctor_json_is_valid_and_no_terminal_escapes(self):
        report = {
            "ok": False,
            "checks": [
                {
                    "name": "network.fbs",
                    "status": "fail",
                    "message": "HTTPS check failed: timeout.",
                    "hint": "Check FBS.",
                }
            ],
        }
        with patch("bot.doctor.diagnose", AsyncMock(return_value=report)):
            code, stdout, stderr = self.invoke("doctor", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout)["ok"])
        self.assertNotIn("\033", stdout + stderr)
        self.assertNotIn(TOKEN, stdout + stderr)

    def test_mutually_incompatible_checks_and_invalid_timeout_are_usage_errors(self):
        for arguments in (
            ["doctor", "--offline", "--session"],
            ["doctor", "--timeout", "0"],
            ["doctor", "--timeout", "61"],
            ["login", "--timeout", "0"],
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_active_instance_does_not_start_another_bot(self):
        with patch(
            "bot.cli.run",
            side_effect=AlreadyRunning(
                "Another Sagasu process is using this browser profile."
            ),
        ):
            code, _, stderr = self.invoke("run")
        self.assertEqual(code, 1)
        self.assertIn("Another Sagasu process", stderr)


class DoctorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / ".env"
        path.write_text(
            f"BOT_TOKEN={TOKEN}\nTELEGRAM_OWNER_ID=42\nSAGASU_BROWSER_PROFILE=profile\nSAGASU_STATE_PATH=state.pickle\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(path)

    async def test_offline_doctor_never_makes_endpoint_or_token_requests(self):
        with (
            patch(
                "bot.doctor.browser_check",
                AsyncMock(return_value=Check("browser", "pass", "Launched.")),
            ),
            patch("bot.doctor.probe", AsyncMock()) as probe,
            patch("bot.doctor.token_check", AsyncMock()) as token,
        ):
            report = await diagnose(self.settings, offline=True)
        self.assertTrue(report["ok"])
        probe.assert_not_awaited()
        token.assert_not_awaited()

    async def test_fbs_timeout_is_distinct_from_token_or_browser_failure(self):
        with (
            patch(
                "bot.doctor.browser_check",
                AsyncMock(return_value=Check("browser", "pass", "Launched.")),
            ),
            patch(
                "bot.doctor.probe",
                AsyncMock(side_effect=[("timeout", None), ("http", 302)]),
            ),
            patch(
                "bot.doctor.token_check",
                AsyncMock(return_value=Check("telegram.token", "pass", "Accepted.")),
            ),
            patch("bot.doctor.session_check", AsyncMock()) as session,
        ):
            report = await diagnose(self.settings, session=True)
        self.assertFalse(report["ok"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(checks["network.fbs"]["status"], "fail")
        self.assertEqual(checks["telegram.token"]["status"], "pass")
        self.assertEqual(checks["session"]["status"], "skip")
        session.assert_not_awaited()


class TerminalTest(unittest.TestCase):
    def test_rattles_animation_is_time_based_and_wraps(self):
        self.assertEqual(DOTS.frame_at(0), DOTS.frames[0])
        self.assertEqual(DOTS.frame_at(80), DOTS.frames[1])
        self.assertEqual(DOTS.frame_at(800), DOTS.frames[0])
        self.assertTrue(all(frame.isascii() for frame in LINE.frames))

    def test_redirected_progress_has_no_animation_and_always_finishes(self):
        stream = io.StringIO()
        reporter = Reporter(stream=stream)
        with reporter.progress("Checking") as progress:
            self.assertIsNone(progress.thread)
            progress.finish("Done")
        self.assertIn("[OK] Done", stream.getvalue())
        self.assertNotIn("\033", stream.getvalue())
        self.assertIsNone(reporter.active)

    def test_token_urls_query_strings_and_exception_details_are_redacted(self):
        record = logging.LogRecord(
            "example",
            logging.ERROR,
            "",
            0,
            f"GET https://api.telegram.org/bot{TOKEN}/getMe https://example.org/login?session=private {TOKEN}",
            (),
            (ValueError, ValueError(TOKEN), None),
        )
        Redact([TOKEN]).filter(record)
        text = record.getMessage()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("session=private", text)
        self.assertIsNone(record.exc_info)


class LockTest(unittest.TestCase):
    def test_kernel_lock_is_exclusive_across_processes_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            code = "from bot.runtime import InstanceLock, AlreadyRunning; import sys\ntry:\n with InstanceLock(sys.argv[1]): pass\nexcept AlreadyRunning: sys.exit(9)"
            with InstanceLock(directory):
                result = subprocess.run(
                    [sys.executable, "-c", code, directory],
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 9)
            result = subprocess.run(
                [sys.executable, "-c", code, directory], capture_output=True, timeout=10
            )
            self.assertEqual(result.returncode, 0)
