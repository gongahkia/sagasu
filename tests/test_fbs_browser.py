import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from sagasu.fbs_client import FBSError, FBSClient, LayoutChanged, SearchConfig


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

    async def test_target_room_is_selected_and_partial_matches_are_deselected(self):
        client, frame, controls = self.selection(selected=1)
        client._room_names.return_value = ["SCIS1 GSR 2-1", "SCIS1 GSR 2-10"]
        self.assertEqual(
            await client._matching_room_names(frame, only_room="SCIS1 GSR 2-1"),
            ["SCIS1 GSR 2-1"],
        )
        first, other = controls.all.return_value
        first.set_checked.assert_awaited_once_with(True, timeout=10000)
        other.set_checked.assert_awaited_once_with(False, timeout=10000)

    async def test_missing_target_does_not_fall_back_to_other_rooms(self):
        client, frame, controls = self.selection()
        self.assertEqual(await client._matching_room_names(frame, only_room="C"), [])
        controls.all.assert_not_awaited()

    async def test_target_search_rejects_extra_selected_rooms(self):
        client, frame, _ = self.selection(selected=2)
        with self.assertRaises(LayoutChanged):
            await client._matching_room_names(frame, only_room="A")

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
            "sagasu.fbs_calendar.read_complete_calendar", AsyncMock(return_value=[])
        ) as reader:
            self.assertEqual(await client._read_availability(frame, ["A"], config), [])
        reader.assert_awaited_once_with(frame, ["A"], config)


class TargetedSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_search_uses_keyword_and_reads_only_target_calendar(self):
        client = FBSClient()
        page = Mock(wait_for_timeout=AsyncMock())
        control = Mock(
            input_value=AsyncMock(return_value=""),
            select_option=AsyncMock(),
            wait_for=AsyncMock(),
            click=AsyncMock(),
        )
        control.first = control
        frame = Mock()
        frame.locator.return_value = control
        client.ensure_authenticated = AsyncMock(return_value=(page, frame))
        client._set_date = AsyncMock()
        client._wait_for_idle = AsyncMock()
        client._set_multi_select = AsyncMock()
        client._set_facility_keyword = AsyncMock()
        client._matching_room_names = AsyncMock(return_value=["Room A"])
        client._read_availability = AsyncMock(return_value=[])
        config = SearchConfig(
            "02-Nov-2026", "14:00", "15:00", floors=("Level 2", "Level 3")
        )
        await client.search(config, room_name="Room A")
        client._set_facility_keyword.assert_awaited_once_with(frame, "Room A")
        client._matching_room_names.assert_awaited_once_with(frame, only_room="Room A")
        client._read_availability.assert_awaited_once_with(frame, ["Room A"], config)
        client._set_multi_select.assert_not_awaited()

    async def test_keyword_uses_visible_search_button_after_focusing_watermark(self):
        client = FBSClient()
        client._wait_for_idle = AsyncMock()
        field = Mock(
            is_visible=AsyncMock(return_value=False),
            wait_for=AsyncMock(),
            fill=AsyncMock(),
        )
        watermark = Mock(count=AsyncMock(return_value=1), click=AsyncMock())
        watermark.first = watermark
        button = Mock(click=AsyncMock())
        frame = Mock()
        frame.locator.side_effect = {
            "input#panel_SimpleSearch_c1": field,
            "#panel_SimpleSearch input:visible": watermark,
            "a#panel_buttonSimpleSearch": button,
        }.__getitem__
        await client._set_facility_keyword(frame, "Room A")
        watermark.click.assert_awaited_once()
        field.fill.assert_awaited_once_with("Room A")
        button.click.assert_awaited_once()


class BrowserCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_timeout_still_stops_driver_and_releases_lock(self):
        import sagasu.fbs_client as module

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
