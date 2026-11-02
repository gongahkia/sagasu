"""The live check is opt-in, session-only, read-only, bounded and redacted."""

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sagasu.cli import main
from sagasu.config import Settings
from sagasu.fbs_client import AuthenticationRequired, RoomAvailability, SearchResult, TimeSlot
from sagasu.smoke import run_smoke


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env = Path(directory.name) / ".env"
        self.env.write_text("SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\n")
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(self.env)
        self.client = Mock(ensure_authenticated=AsyncMock(), search=AsyncMock(), book=AsyncMock())
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=False)

    def test_no_network_or_browser_without_live_flag(self):
        with patch("sagasu.smoke.FBSClient") as browser, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["smoke"])
        self.assertEqual(error.exception.code, 2)
        browser.assert_not_called()

    async def test_only_authenticated_search_and_account_reads_are_called(self):
        from sagasu.fbs_client import search_config_from_preferences
        config = search_config_from_preferences({})
        self.client.search.return_value = SearchResult(config, [RoomAvailability("A", [TimeSlot(config.start_time, config.end_time, "free")])], "fixture")
        with patch("sagasu.smoke.FBSClient", return_value=self.client) as constructor, patch("sagasu.smoke.read_bookings", AsyncMock(return_value={"bookings": [{"private": "COBOOKER_SECRET"}], "total": 1, "complete": False})) as bookings:
            report = await run_smoke(self.settings)
        self.assertTrue(report["ok"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["checks"][-1]["complete"])
        self.assertNotIn("COBOOKER_SECRET", json.dumps(report))
        constructor.assert_called_once_with(interactive=False, keep_alive=False, profile_path=self.settings.profile)
        self.client.ensure_authenticated.assert_awaited_once()
        bookings.assert_awaited_once_with(self.client)
        self.client.book.assert_not_awaited()

    async def test_expired_session_stops_without_search_or_credential_submission(self):
        self.client.ensure_authenticated.side_effect = AuthenticationRequired("private cookie and account identity")
        with patch("sagasu.smoke.FBSClient", return_value=self.client), patch("sagasu.smoke.read_bookings", AsyncMock()) as bookings:
            report = await run_smoke(self.settings)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["error_code"], "AuthenticationRequired")
        self.assertNotIn("private cookie", json.dumps(report))
        self.client.search.assert_not_awaited()
        bookings.assert_not_awaited()

    async def test_deadline_closes_browser_and_releases_profile(self):
        async def wait_forever():
            await asyncio.Event().wait()
        self.client.ensure_authenticated.side_effect = wait_forever
        with patch("sagasu.smoke.FBSClient", return_value=self.client):
            report = await run_smoke(self.settings, timeout=0.01)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["error_code"], "TimeoutError")
        self.client.__aexit__.assert_awaited_once()
        from sagasu.runtime import InstanceLock
        with InstanceLock(self.settings.profile):
            pass
