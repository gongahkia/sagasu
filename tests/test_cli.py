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


from sagasu import __version__
from sagasu._vendor.rattles import DOTS, LINE
from sagasu.cli import main, parser
from sagasu.config import ConfigError, Settings
from sagasu.console import Redact, Reporter
from sagasu.doctor import Check, diagnose
from sagasu.runtime import AlreadyRunning, InstanceLock

TOKEN = "123456789:abcdefghijklmnopqrstuvwxyz_123456789"


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
            f"GET https://example.org/private/{TOKEN} https://example.org/login?session=private {TOKEN}",
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
            code = "from sagasu.runtime import InstanceLock, AlreadyRunning; import sys\ntry:\n with InstanceLock(sys.argv[1]): pass\nexcept AlreadyRunning: sys.exit(9)"
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


class StartupTest(unittest.TestCase):

    def test_rotating_logs_keep_private_permissions_and_redaction(self):
        from sagasu.console import PrivateRotatingFileHandler

        with tempfile.TemporaryDirectory() as directory:
            handler = PrivateRotatingFileHandler(
                Path(directory) / "sagasu.log",
                maxBytes=80,
                backupCount=2,
                encoding="utf-8",
            )
            handler.addFilter(Redact([TOKEN]))
            try:
                for index in range(6):
                    handler.handle(
                        logging.LogRecord(
                            "test",
                            logging.WARNING,
                            "",
                            0,
                            f"{index} token {TOKEN} and a sufficiently long message for rollover",
                            (),
                            None,
                        )
                    )
            finally:
                handler.close()
            files = list(Path(directory).glob("sagasu.log*"))
            self.assertGreater(len(files), 1)
            for path in files:
                self.assertNotIn(TOKEN, path.read_text())
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ascii_tty_falls_back_and_spinner_thread_is_joined(self):
        class Terminal(io.StringIO):
            @property
            def encoding(self):
                return "ascii"

            def isatty(self):
                return True

        stream = Terminal()
        with patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            reporter = Reporter(stream=stream, no_color=True)
            self.assertEqual(reporter.rattle, LINE)
            with reporter.progress("Checking") as progress:
                progress.finish("Done")
            self.assertFalse(progress.thread.is_alive())
            self.assertIsNone(reporter.active)
