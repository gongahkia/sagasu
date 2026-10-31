import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bot.fbs_client import AuthenticationRequired
from bot.scraper_session import (
    ProfileBusy,
    ScraperClient,
    profile_lock,
    scraper_profile,
)


class ProfileTest(unittest.TestCase):
    def test_lock_is_exclusive_across_processes_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            code = (
                "from pathlib import Path; "
                "from bot.scraper_session import profile_lock; "
                f"\nwith profile_lock(Path({directory!r})): pass"
            )
            with profile_lock(directory):
                result = subprocess.run(
                    [sys.executable, "-c", code], capture_output=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"ProfileBusy", result.stderr)
                with self.assertRaises(ProfileBusy):
                    with profile_lock(directory):
                        pass
            subprocess.run(
                [sys.executable, "-c", code], check=True, capture_output=True
            )

    def test_scraper_cannot_use_bot_profile(self):
        with patch.dict(os.environ, {"SAGASU_SCRAPER_PROFILE": "bot/.fbs-profile"}):
            with self.assertRaises(ValueError):
                scraper_profile()

    def test_default_profile_is_ignored_and_separate(self):
        with patch.dict(os.environ, {}, clear=True):
            profile = scraper_profile()
        self.assertEqual(profile.name, "scraper-profile")
        self.assertEqual(profile.parent.name, "artifacts")


class SessionTest(unittest.IsolatedAsyncioTestCase):
    def client(self, *, ready=True, manual=False):
        client = ScraperClient(manual_login=manual, login_timeout=0.02)
        page = Mock()
        page.goto = AsyncMock()
        page.is_closed.return_value = False
        page.url = "https://fbs.intranet.smu.edu.sg/home"
        frame = Mock()
        frame.url = "https://fbs.intranet.smu.edu.sg/search"
        frame.locator.return_value.wait_for = AsyncMock()
        page.frame.return_value = frame if ready else None
        client.page = page
        return client

    async def test_readiness_requires_search_controls(self):
        client = self.client()
        page, frame = await client.ensure_authenticated()
        self.assertIs(page, client.page)
        self.assertEqual(frame.locator.return_value.wait_for.await_count, 5)

    async def test_manual_login_shell_without_controls_times_out(self):
        client = self.client(ready=False, manual=True)
        with self.assertRaises(AuthenticationRequired):
            await client.ensure_authenticated()

    async def test_manual_login_waits_through_redirect_to_ready_fbs(self):
        client = self.client(manual=True)
        client.page.url = "https://login.example.invalid/"

        async def redirect(_):
            client.page.url = "https://fbs.intranet.smu.edu.sg/home"

        with patch("bot.scraper_session.asyncio.sleep", side_effect=redirect):
            await client.ensure_authenticated()

    async def test_expired_session_does_not_submit_credentials(self):
        client = self.client()
        client.page.url = "https://login.example.invalid/private-login"
        with patch(
            "bot.password_auth.password_sign_in", new_callable=AsyncMock
        ) as password:
            with self.assertRaises(AuthenticationRequired):
                await client.ensure_authenticated()
            password.assert_not_awaited()
        client.page.locator.assert_not_called()
        self.assertFalse(client.interactive)
        self.assertFalse(client.unattended)
        self.assertFalse(client.keep_alive)
        self.assertTrue(client.strict_availability)

    async def test_scraper_cannot_call_booking_path(self):
        with self.assertRaises(RuntimeError):
            await self.client().book(None, submit=True)

    async def test_navigation_error_does_not_expose_url(self):
        client = self.client()
        client.page.goto.side_effect = RuntimeError("private-login-url")
        with self.assertRaises(AuthenticationRequired) as error:
            await client.ensure_authenticated()
        self.assertNotIn("private-login", str(error.exception))


class ScrapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_seven_days_reuse_dedicated_profile_and_close_browser(self):
        from bot.scrape_daily import scrape
        from bot.fbs_client import SearchResult

        with tempfile.TemporaryDirectory() as directory:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.search.side_effect = lambda config: SearchResult(
                config, [], "private"
            )
            with (
                patch("bot.scrape_daily.scraper_profile", return_value=Path(directory)),
                patch("bot.scrape_daily.ScraperClient", return_value=client) as factory,
                patch("builtins.print"),
            ):
                snapshot = await scrape()
                self.assertEqual(len(snapshot["days"]), 7)
                self.assertEqual(client.search.await_count, 7)
                self.assertEqual(
                    factory.call_args.kwargs["profile_path"], Path(directory)
                )
                self.assertTrue(factory.call_args.kwargs["headless"])
                client.__aexit__.assert_awaited_once()
            # The lock is released along with the browser context.
            with profile_lock(directory):
                pass

    async def test_partial_scrape_closes_browser_and_releases_profile(self):
        from bot.scrape_daily import scrape
        from bot.fbs_client import SearchResult
        from bot.snapshot import search_days

        with tempfile.TemporaryDirectory() as directory:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.search.side_effect = [
                SearchResult(search_days()[0], [], "private"),
                AuthenticationRequired("expired"),
            ]
            with (
                patch("bot.scrape_daily.scraper_profile", return_value=Path(directory)),
                patch("bot.scrape_daily.ScraperClient", return_value=client),
                patch("builtins.print"),
            ):
                with self.assertRaises(AuthenticationRequired):
                    await scrape()
                self.assertEqual(client.search.await_count, 2)
                client.__aexit__.assert_awaited_once()
            with profile_lock(directory):
                pass
