import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.fbs_client import AuthenticationRequired, FBSError, FBSClient


class SessionTest(unittest.IsolatedAsyncioTestCase):
    def test_password_manager_disabled_without_losing_other_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FBSClient(profile_path=Path(directory))
            profile = Path(directory) / "Default"
            profile.mkdir()
            preferences = profile / "Preferences"
            preferences.write_text(
                json.dumps(
                    {
                        "profile": {"existing": "keep"},
                        "unrelated": {"keep": True},
                        "credentials_enable_service": True,
                    }
                )
            )
            client._configure_password_manager()
            result = json.loads(preferences.read_text())
            self.assertFalse(result["credentials_enable_service"])
            self.assertFalse(result["credentials_enable_autosignin"])
            self.assertFalse(result["profile"]["password_manager_enabled"])
            self.assertEqual(result["profile"]["existing"], "keep")
            self.assertEqual(result["unrelated"], {"keep": True})
            if os.name != "nt":
                self.assertEqual(preferences.stat().st_mode & 0o777, 0o600)

    def test_invalid_preferences_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FBSClient(profile_path=Path(directory))
            profile = Path(directory) / "Default"
            profile.mkdir()
            preferences = profile / "Preferences"
            preferences.write_text("private corrupted preferences")
            with self.assertRaises(FBSError) as error:
                client._configure_password_manager()
            self.assertNotIn("private corrupted", str(error.exception))
            self.assertEqual(preferences.read_text(), "private corrupted preferences")

    async def test_private_session_cookies_keep_original_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FBSClient(profile_path=Path(directory))
            client.context = AsyncMock()
            client.context.cookies.return_value = [
                {"name": "session", "value": "private", "expires": -1},
                {"name": "persistent", "value": "private", "expires": 2000000000},
            ]
            await client._save_session()
            saved = json.loads(client.session_path.read_text())
            self.assertEqual([cookie["name"] for cookie in saved], ["session"])
            self.assertEqual(saved[0]["expires"], -1)
            if os.name != "nt":
                self.assertEqual(client.session_path.stat().st_mode & 0o777, 0o600)
            await client._restore_session()
            client.context.add_cookies.assert_awaited_once_with(saved)

    async def test_failed_save_retains_previous_session(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FBSClient(profile_path=Path(directory))
            client.session_path.write_text('[{"old": true}]')
            client.context = AsyncMock()
            client.context.cookies.side_effect = RuntimeError("private details")
            with self.assertRaises(FBSError) as error:
                await client._save_session()
            self.assertNotIn("private details", str(error.exception))
            self.assertEqual(
                json.loads(client.session_path.read_text()), [{"old": True}]
            )

    async def test_noninteractive_check_rejects_corrupt_session_without_details(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FBSClient(profile_path=Path(directory), interactive=False)
            client.context = AsyncMock()
            client.session_path.write_text("private invalid JSON")
            with self.assertRaises(AuthenticationRequired) as error:
                await client._restore_session()
            self.assertNotIn("private invalid", str(error.exception))
            client.context.add_cookies.assert_not_awaited()

    async def test_manual_login_can_repair_corrupt_session(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FBSClient(profile_path=Path(directory), interactive=True)
            client.context = AsyncMock()
            client.session_path.write_text("invalid JSON")
            await client._restore_session()
            client.context.add_cookies.assert_not_awaited()

    async def test_shared_browser_closes_and_globals_are_cleared(self):
        import bot.fbs_client as module

        context, playwright = AsyncMock(), AsyncMock()
        with (
            patch.object(module, "_SHARED_CONTEXT", context),
            patch.object(module, "_SHARED_PLAYWRIGHT", playwright),
        ):
            await module.close_shared_browser()
            self.assertIsNone(module._SHARED_CONTEXT)
            self.assertIsNone(module._SHARED_PLAYWRIGHT)
        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    def test_profile_environment_is_read_when_client_is_created(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"SAGASU_BROWSER_PROFILE": directory}),
        ):
            self.assertEqual(FBSClient().profile_path, Path(directory))
