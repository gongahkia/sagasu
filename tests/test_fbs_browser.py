import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from bot.fbs_client import FBSError, FBSClient, LayoutChanged, SearchConfig


class NavigationTest(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_timeout_reports_connectivity_without_attempting_login(
        self,
    ):
        client = FBSClient()
        client.page = AsyncMock()
        client.page.goto.side_effect = PlaywrightTimeoutError(
            "private redirect details"
        )
        client._complete_interactive_authentication = AsyncMock()
        with self.assertRaises(FBSError) as error:
            await client.ensure_authenticated()
        self.assertIn("navigation timed out", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        client._complete_interactive_authentication.assert_not_awaited()

    async def test_browser_navigation_error_does_not_expose_raw_details(self):
        client = FBSClient()
        client.page = AsyncMock()
        client.page.goto.side_effect = PlaywrightError("private redirect details")
        with self.assertRaises(FBSError) as error:
            await client.ensure_authenticated()
        self.assertIn("could not open FBS", str(error.exception))
        self.assertNotIn("private", str(error.exception))


class RoomSelectionTest(unittest.IsolatedAsyncioTestCase):
    def selection(self, *, count=2, selected=2):
        client = FBSClient()
        client._wait_for_idle = AsyncMock()
        client._room_names = AsyncMock(return_value=["A", "B"])
        frame = Mock()
        pagination = Mock()
        pagination.count = AsyncMock(return_value=0)
        controls = Mock()
        controls.count = AsyncMock(return_value=count)
        controls.all = AsyncMock(return_value=[AsyncMock() for _ in range(count)])
        controls.evaluate_all = AsyncMock(return_value=selected)
        frame.locator.side_effect = lambda selector: (
            pagination if "Page$" in selector else controls
        )
        return client, frame, controls

    async def test_every_matching_room_is_selected_before_reading_calendar(self):
        client, frame, controls = self.selection()
        self.assertEqual(await client._matching_room_names(frame), ["A", "B"])
        for checkbox in controls.all.return_value:
            checkbox.check.assert_awaited_once()

    async def test_missing_selection_control_is_rejected(self):
        client, frame, controls = self.selection(count=1)
        with self.assertRaises(LayoutChanged):
            await client._matching_room_names(frame)
        controls.all.assert_not_awaited()

    async def test_failed_selection_cannot_mark_unselected_room_as_free(self):
        client, frame, _ = self.selection(selected=1)
        with self.assertRaises(LayoutChanged):
            await client._matching_room_names(frame)

    async def test_changed_results_during_selection_are_rejected(self):
        client, frame, _ = self.selection()
        client._room_names.side_effect = [["A", "B"], ["A", "C"]]
        with self.assertRaises(LayoutChanged):
            await client._matching_room_names(frame)

    async def test_paginated_results_are_rejected(self):
        client = FBSClient()
        frame = Mock()
        frame.locator.return_value.count = AsyncMock(return_value=2)
        with self.assertRaises(LayoutChanged):
            await client._matching_room_names(frame)

    async def test_no_matching_rooms_needs_no_selection(self):
        client, frame, controls = self.selection(count=0, selected=0)
        client._room_names.return_value = []
        self.assertEqual(await client._matching_room_names(frame), [])
        controls.all.assert_not_awaited()

    async def test_bot_uses_complete_calendar_reader(self):
        client = FBSClient()
        client._wait_for_idle = AsyncMock()
        frame = Mock()
        config = SearchConfig("01-Nov-2026", "10:00", "12:00")
        with patch(
            "bot.fbs_calendar.read_complete_calendar", AsyncMock(return_value=[])
        ) as reader:
            self.assertEqual(await client._read_availability(frame, ["A"], config), [])
        reader.assert_awaited_once_with(frame, ["A"], config)


class BrowserCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_timeout_still_stops_driver_and_releases_lock(self):
        import bot.fbs_client as module

        client = FBSClient(keep_alive=False)
        client.context = AsyncMock()
        client.context.close.side_effect = TimeoutError()
        client._playwright = AsyncMock()
        await module._BROWSER_LOCK.acquire()
        with self.assertRaises(TimeoutError):
            await client.__aexit__(None, None, None)
        client._playwright.stop.assert_awaited_once()
        self.assertFalse(module._BROWSER_LOCK.locked())

    async def test_empty_filters_clear_previous_search_restrictions(self):
        client = FBSClient()
        frame = Mock()
        locator = Mock()
        locator.first = locator
        locator.input_value = AsyncMock(return_value="old building")
        locator.click = AsyncMock()
        locator.wait_for = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        locator.locator.return_value = locator
        frame.locator.return_value = locator
        client._wait_for_idle = AsyncMock()
        await client._set_multi_select(frame, "DropMultiBuildingList_c1", ())
        self.assertIn(
            'input[type="button"][value="Clear"]:visible',
            [call.args[0] for call in locator.locator.call_args_list],
        )
        self.assertEqual(locator.click.await_count, 4)
