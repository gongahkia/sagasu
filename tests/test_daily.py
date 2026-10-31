import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.fbs_client import (
    AuthenticationRequired,
    FBSClient,
    LayoutChanged,
    RoomAvailability,
    SearchResult,
    TimeSlot,
)
from bot.password_auth import PasswordLoginError, password_sign_in
from bot.snapshot import SGT, public_snapshot, search_days, write_json
from scripts.build_site import build
from scripts.publish_snapshot import merge


class SnapshotTest(unittest.TestCase):
    def results(self):
        return [
            SearchResult(
                config,
                [
                    RoomAvailability(
                        "SCIS GSR",
                        [
                            TimeSlot(
                                "08:00",
                                "22:00",
                                "free",
                                "SECRET PERSON person@smu.edu.sg",
                            )
                        ],
                    )
                ],
                "private-source-timestamp",
            )
            for config in search_days(datetime(2026, 12, 31, 23, tzinfo=SGT))
        ]

    def test_dates_and_campus_wide_scope(self):
        configs = search_days(datetime.fromisoformat("2026-12-31T18:00:00+00:00"))
        self.assertEqual(configs[0].date, "01-Jan-2027")
        self.assertEqual(configs[-1].date, "07-Jan-2027")
        for config in configs:
            self.assertEqual(config.buildings, ())
            self.assertEqual(config.floors, ())
            self.assertEqual(config.facility_types, ("Group Study Room",))
            self.assertEqual((config.start_time, config.end_time), ("08:00", "22:00"))

    def test_public_allowlist_strips_private_event_details(self):
        snapshot = public_snapshot(self.results())
        serialized = json.dumps(snapshot)
        for forbidden in ("SECRET", "person@", "details", "private-source"):
            self.assertNotIn(forbidden, serialized)
        slot = snapshot["days"][0]["rooms"][0]["timeslots"][0]
        self.assertEqual(set(slot), {"start", "end", "status"})

    def test_partial_scrape_is_rejected_and_empty_results_are_valid(self):
        with self.assertRaises(ValueError):
            public_snapshot(self.results()[:6])
        results = self.results()
        for result in results:
            result.rooms = []
        self.assertTrue(
            all(day["rooms"] == [] for day in public_snapshot(results)["days"])
        )

    def test_failure_retains_snapshot_even_if_attempt_has_partial_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt, retained = root / "attempt", root / "retained"
            original = public_snapshot(self.results())
            write_json(retained / "snapshot.json", original)
            write_json(attempt / "snapshot.json", {"days": []})
            write_json(attempt / "status.json", {"status": "authentication_required"})
            merge(attempt, retained)
            self.assertEqual(
                json.loads((retained / "snapshot.json").read_text()), original
            )
            self.assertEqual(
                json.loads((retained / "status.json").read_text())["status"],
                "authentication_required",
            )

    def test_success_replaces_snapshot_and_build_copies_only_public_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt, retained = root / "attempt", root / "retained"
            retained.mkdir()
            write_json(attempt / "snapshot.json", public_snapshot(self.results()))
            write_json(attempt / "status.json", {"status": "ok"})
            (attempt / "credentials.json").write_text("secret")
            merge(attempt, retained)
            build(root / "dist", retained)
            self.assertEqual(
                set(path.name for path in (root / "dist/data").iterdir()),
                {"snapshot.json", "status.json"},
            )
            self.assertTrue((root / "dist/assets/icon_no_words.jpg").is_file())
            self.assertTrue((root / "dist/setup.html").is_file())

    def test_first_run_build_contains_no_sample_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build(root / "dist", root / "missing")
            self.assertIsNone(
                json.loads((root / "dist/data/snapshot.json").read_text())
            )


class FakeLocator:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind
        self.first = self

    async def count(self):
        visible = {
            "dashboard": self.page.state == "done",
            "email": self.page.state == "email",
            "password": self.page.state == "password",
            "alternate": self.page.state == "alternate",
            "password_option": self.page.state == "method",
        }
        return int(visible.get(self.kind, False))

    async def inner_text(self, **kwargs):
        return {
            "mfa": "Approve a request in Microsoft Authenticator",
            "rejected": "Your account or password is incorrect",
        }.get(self.page.state, "Sign in")

    async def fill(self, value):
        self.page.entered.append(self.kind)

    async def click(self):
        transitions = {
            "email": "alternate",
            "alternate": "method",
            "method": "password",
            "password": self.page.after_password,
        }
        self.page.state = transitions[self.page.state]

    async def all(self):
        return [self] if await self.count() else []

    async def is_visible(self):
        return bool(await self.count())


class FakePage:
    def __init__(self, state="email", after_password="done"):
        self.state, self.after_password = state, after_password
        self.entered = []
        self.keyboard = AsyncMock()

    def locator(self, selector):
        if selector == "iframe#frameBottom":
            kind = "dashboard"
        elif selector == "body":
            kind = "body"
        elif "password" in selector:
            kind = "password"
        elif "loginfmt" in selector:
            kind = "email"
        else:
            kind = "submit"
        return FakeLocator(self, kind)

    def get_by_text(self, pattern):
        return FakeLocator(
            self,
            "alternate" if pattern.search("Sign in another way") else "password_option",
        )

    async def wait_for_timeout(self, duration):
        pass


class PasswordAuthTest(unittest.IsolatedAsyncioTestCase):
    async def test_alternate_password_route(self):
        page = FakePage()
        progress = AsyncMock()
        with patch.dict(
            os.environ,
            {
                "SMU_LOGIN_EMAIL": "fixture@smu.edu.sg",
                "SMU_LOGIN_PASSWORD": "fixture-only",
            },
        ):
            await password_sign_in(page, progress=progress)
        self.assertEqual(page.state, "done")
        self.assertEqual(page.entered, ["email", "password"])
        labels = [call.args[0] for call in progress.call_args_list]
        self.assertIn("login: selecting_password_route", labels)
        self.assertEqual(labels[-1], "login: authenticated")
        self.assertNotIn("fixture", str(labels))

    async def test_mfa_and_rejected_credentials_fail_without_approval(self):
        for state in ("mfa", "rejected"):
            page = FakePage(after_password=state)
            with patch.dict(
                os.environ,
                {
                    "SMU_LOGIN_EMAIL": "fixture@smu.edu.sg",
                    "SMU_LOGIN_PASSWORD": "fixture-only",
                },
            ):
                with self.assertRaises(PasswordLoginError) as error:
                    await password_sign_in(page)
            self.assertEqual(
                error.exception.code,
                "interactive_verification" if state == "mfa" else "rejected",
            )
            self.assertEqual(page.state, state)

    async def test_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AuthenticationRequired):
                await password_sign_in(FakePage())

    async def test_browser_exception_is_sanitized(self):
        page = FakePage()
        page.locator = unittest.mock.Mock(
            side_effect=RuntimeError("private credential content")
        )
        with patch.dict(
            os.environ, {"SMU_LOGIN_EMAIL": "fixture", "SMU_LOGIN_PASSWORD": "secret"}
        ):
            with self.assertRaises(AuthenticationRequired) as error:
                await password_sign_in(page)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(error.exception.code, "browser_error")

    async def test_expired_deadline_reports_a_fixed_code(self):
        with patch.dict(
            os.environ, {"SMU_LOGIN_EMAIL": "fixture", "SMU_LOGIN_PASSWORD": "secret"}
        ), patch(
            "bot.password_auth._password_flow", AsyncMock(side_effect=TimeoutError())
        ):
            with self.assertRaises(PasswordLoginError) as error:
                await password_sign_in(FakePage())
        self.assertEqual(error.exception.code, "timeout")


class BrowserContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_unrecognized_calendar_event_is_not_treated_as_free(self):
        client = FBSClient(strict_availability=True)
        header = AsyncMock()
        header.inner_text.return_value = "GSR A"
        header.bounding_box.return_value = {"y": 100, "height": 30}
        event = AsyncMock()
        event.get_attribute.return_value = "Unexpected FBS event format"
        event.bounding_box.return_value = {"y": 100, "height": 30}
        headers, events = unittest.mock.Mock(), unittest.mock.Mock()
        headers.all = AsyncMock(return_value=[header])
        events.all = AsyncMock(return_value=[event])
        frame = unittest.mock.Mock()
        frame.locator.side_effect = [headers, events]
        with self.assertRaises(LayoutChanged):
            await client._read_availability(frame, ["GSR A"], search_days()[0])

    async def test_empty_multiselect_clears_previous_restrictions(self):
        client = FBSClient()
        frame = unittest.mock.Mock()
        locator = unittest.mock.Mock()
        locator.first = locator
        locator.input_value = AsyncMock(return_value="selected old building")
        locator.click = AsyncMock()
        locator.wait_for = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        locator.locator.return_value = locator
        frame.locator.return_value = locator
        client._wait_for_idle = AsyncMock()
        await client._set_multi_select(frame, "DropMultiBuildingList_c1", ())
        selector_calls = [call.args[0] for call in locator.locator.call_args_list]
        self.assertIn('input[type="button"][value="Clear"]:visible', selector_calls)
        self.assertEqual(locator.click.await_count, 4)

    async def test_strict_discovery_rejects_pagination(self):
        client = FBSClient(strict_availability=True)
        frame = unittest.mock.Mock()
        frame.locator.return_value.count = AsyncMock(return_value=2)
        with self.assertRaises(LayoutChanged):
            await client._matching_room_names(frame)

    async def test_job_browser_is_closed_even_on_exception(self):
        import bot.fbs_client as module

        client = FBSClient(keep_alive=False)
        client.context = AsyncMock()
        client._playwright = AsyncMock()
        await module._BROWSER_LOCK.acquire()
        await client.__aexit__(RuntimeError, RuntimeError("test"), None)
        client.context.close.assert_awaited_once()
        client._playwright.stop.assert_awaited_once()
        self.assertFalse(module._BROWSER_LOCK.locked())


class ScraperCommandTest(unittest.TestCase):
    def test_login_failure_reports_code_and_headed_flags_reach_scraper(self):
        from bot.scrape_daily import main

        with tempfile.TemporaryDirectory() as directory:
            mocked = AsyncMock(
                side_effect=PasswordLoginError(
                    "interactive_verification", "verification_prompt"
                )
            )
            with (
                patch(
                    "sys.argv",
                    ["scrape_daily", "--headed", "--verbose", "--output", directory],
                ),
                patch("bot.scrape_daily.scrape", mocked),
                patch("builtins.print"),
            ):
                self.assertEqual(main(), 1)
            mocked.assert_awaited_once_with(headed=True, verbose=True)
            status = json.loads((Path(directory) / "status.json").read_text())
            self.assertEqual(status["error_code"], "interactive_verification")
            self.assertEqual(status["login_stage"], "verification_prompt")

    def test_failed_attempt_has_sanitized_status_and_no_leftover_snapshot(self):
        from bot.scrape_daily import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "snapshot.json", {"old": "incomplete"})
            with (
                patch("sys.argv", ["scrape_daily", "--output", directory]),
                patch(
                    "bot.scrape_daily.scrape",
                    AsyncMock(side_effect=LayoutChanged("private page content")),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(main(), 1)
            self.assertFalse((root / "snapshot.json").exists())
            status = json.loads((root / "status.json").read_text())
            self.assertEqual(status["status"], "layout_changed")
            self.assertNotIn("private", json.dumps(status))
