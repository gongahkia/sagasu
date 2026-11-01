import unittest
from unittest.mock import AsyncMock, Mock

from bot.fbs_client import (
    BookingRequest,
    BookingUnavailable,
    BookingOutcomeUnknown,
    CalendarSelectionLost,
    FBSError,
    FBSClient,
    LayoutChanged,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
    calendar_time_x,
)


class CalendarGeometryTest(unittest.TestCase):
    def test_half_hour_boundaries_interpolate_inside_hour_headers(self):
        headers = [
            {"label": "11:00", "x": 100, "width": 50},
            {"label": "12:00", "x": 150, "width": 50},
            {"label": "13:00", "x": 200, "width": 50},
        ]
        self.assertEqual(calendar_time_x(headers, "11:00", 60), 100)
        self.assertEqual(calendar_time_x(headers, "11:30", 60), 125)
        self.assertEqual(calendar_time_x(headers, "13:30", 60), 225)
        self.assertEqual(calendar_time_x(headers, "14:00", 60), 250)

    def test_cell_headers_are_supported(self):
        headers = [
            {"label": "11:00", "x": 100, "width": 25},
            {"label": "11:30", "x": 125, "width": 25},
        ]
        self.assertEqual(calendar_time_x(headers, "11:30", 30), 125)
        self.assertEqual(calendar_time_x(headers, "12:00", 30), 150)

    def test_missing_duplicate_or_unreadable_geometry_is_rejected(self):
        base = {"label": "11:00", "x": 100, "width": 50}
        for headers in (
            [],
            [base, base],
            [{**base, "width": 0}],
            [{**base, "x": float("nan")}],
            [{**base, "label": "Monday"}],
        ):
            with self.subTest(headers=headers), self.assertRaises(LayoutChanged):
                calendar_time_x(headers, "11:00", 60)


class CalendarDragTest(unittest.IsolatedAsyncioTestCase):
    def setup_calendar(self):
        client = FBSClient()
        client.page = Mock(mouse=AsyncMock())
        client._wait_for_idle = AsyncMock()
        client._verify_calendar_selection = AsyncMock()
        client._calendar_row_box = AsyncMock(return_value={"y": 220, "height": 20})
        scroll = Mock(
            count=AsyncMock(return_value=1),
            bounding_box=AsyncMock(
                return_value={"x": 100, "y": 200, "width": 200, "height": 100}
            ),
        )
        state = {"left": 0}

        async def evaluate(script, value=None):
            if value is not None:
                state["left"] = value
                return None
            return {"left": state["left"], "width": 200, "full": 1200}

        scroll.evaluate = AsyncMock(side_effect=evaluate)
        frame = Mock(wait_for_timeout=AsyncMock())
        frame.locator.return_value = scroll

        async def headers(_):
            return (
                [
                    {
                        "label": f"{hour:02d}:00",
                        "x": 100 + hour * 50 - state["left"],
                        "width": 50,
                    }
                    for hour in range(24)
                ],
                60,
            )

        client._calendar_headers = AsyncMock(side_effect=headers)
        return client, frame, scroll, state

    async def test_offscreen_range_is_scrolled_into_view_before_mouse_drag(self):
        client, frame, scroll, state = self.setup_calendar()
        await client._drag_timeslot(frame, "Room A", "18:00", "19:00")
        self.assertGreater(state["left"], 0)
        moves = client.page.mouse.move.await_args_list
        self.assertTrue(102 <= moves[0].args[0] < moves[1].args[0] <= 298)
        self.assertEqual(moves[0].args[1], 230)
        client.page.mouse.down.assert_awaited_once()
        client.page.mouse.up.assert_awaited_once()

    async def test_row_near_bottom_is_centered_to_prevent_drag_autoscroll(self):
        client = FBSClient()
        state = {"top": 0}
        scroll = Mock(bounding_box=AsyncMock(return_value={"y": 200, "height": 100}))

        async def evaluate(script, value=None):
            if value is not None:
                state["top"] = value
                return
            if "scrollTop = 0" in script:
                state["top"] = 0
                return
            return {"top": state["top"], "height": 100, "full": 300}

        scroll.evaluate = AsyncMock(side_effect=evaluate)
        row = Mock(
            inner_text=AsyncMock(return_value="Room A"),
            bounding_box=AsyncMock(
                side_effect=lambda: {"y": 280 - state["top"], "height": 20}
            ),
        )
        frame = Mock(wait_for_timeout=AsyncMock())
        frame.locator.return_value.all = AsyncMock(return_value=[row])
        box = await client._calendar_row_box(frame, scroll, "Room A")
        self.assertEqual(state["top"], 40)
        self.assertEqual(box["y"] + box["height"] / 2, 250)

    async def test_range_too_wide_never_starts_a_drag(self):
        client, frame, _, _ = self.setup_calendar()
        with self.assertRaises(LayoutChanged):
            await client._drag_timeslot(frame, "Room A", "01:00", "20:00")
        client.page.mouse.down.assert_not_awaited()

    async def test_mouse_is_released_when_drag_fails(self):
        client, frame, _, _ = self.setup_calendar()
        client.page.mouse.move.side_effect = [None, RuntimeError("interrupted")]
        with self.assertRaises(RuntimeError):
            await client._drag_timeslot(frame, "Room A", "18:00", "19:00")
        client.page.mouse.up.assert_awaited_once()


class ValidationBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_validate_only_never_clicks_final_confirmation(self):
        client = FBSClient()
        client._wait_for_idle = AsyncMock()
        search = SearchConfig("02-Nov-2026", "18:00", "19:00")
        request = BookingRequest(
            search=search,
            room="Room A",
            purpose="Meeting",
            co_booker="friend@smu.edu.sg",
        )
        client.search = AsyncMock(
            return_value=SearchResult(
                search,
                [RoomAvailability("Room A", [TimeSlot("18:00", "19:00", "free")])],
                "test",
            )
        )
        calendar = Mock()
        calendar.locator.return_value.click = AsyncMock()
        client.page = Mock()
        client.page.frame.return_value = calendar
        client._drag_timeslot = AsyncMock()
        client._add_co_booker = AsyncMock()
        client._verify_booking = AsyncMock()
        client._verify_booking_details = AsyncMock()
        controls = {}

        def locator(selector):
            control = Mock(
                fill=AsyncMock(),
                select_option=AsyncMock(),
                check=AsyncMock(),
                uncheck=AsyncMock(),
                click=AsyncMock(),
            )
            control.first = control
            controls[selector] = control
            return control

        booking = Mock()
        booking.locator.side_effect = locator
        client._prepare_booking_frame = AsyncMock(return_value=booking)
        result = await client.book(request, submit=False)
        client.search.assert_awaited_once_with(search, room_name=request.room)
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("a#panel_UIButton2", controls)
        client._verify_booking.assert_not_awaited()
        client._verify_booking_details.assert_awaited_once()
        # Once final confirmation is attempted, a verification failure is an
        # uncertain outcome, never a claim that no booking was submitted.
        client.page.wait_for_timeout = AsyncMock()
        client._verify_booking.side_effect = FBSError("Could not verify")
        with self.assertRaises(BookingOutcomeUnknown):
            await client.book(request, submit=True)
        self.assertEqual(client.search.await_args.kwargs, {"room_name": request.room})
        controls["a#panel_UIButton2"].click.assert_awaited_once()


class BookingDetailsTest(unittest.IsolatedAsyncioTestCase):
    async def test_server_room_and_full_datetimes_must_match_request(self):
        client = FBSClient()
        request = BookingRequest(
            SearchConfig("02-Nov-2026", "14:00", "15:00"),
            "Room A",
            "Meeting",
            "friend@smu.edu.sg",
        )
        frame = Mock()
        labels = Mock(
            all=AsyncMock(return_value=[Mock(is_visible=AsyncMock(return_value=True))])
        )
        frame.get_by_text.return_value = labels
        fields = {
            "select#bookingFormControl1_DropDownStartTime_c1": Mock(
                input_value=AsyncMock(return_value="11/2/2026 2:00:00 PM")
            ),
            "select#bookingFormControl1_DropDownEndTime_c1": Mock(
                input_value=AsyncMock(return_value="11/2/2026 3:00:00 PM")
            ),
        }
        frame.locator.side_effect = fields.get
        await client._verify_booking_details(frame, request)
        fields[
            "select#bookingFormControl1_DropDownEndTime_c1"
        ].input_value.return_value = "11/2/2026 3:30:00 PM"
        with self.assertRaises(BookingUnavailable):
            await client._verify_booking_details(frame, request)
        labels.all.return_value = []
        with self.assertRaises(BookingUnavailable):
            await client._verify_booking_details(frame, request)


class CoBookerControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_watermark_focused_before_search_and_added_table_matches_email(self):
        from unittest.mock import call

        client = FBSClient()
        client._wait_for_idle = AsyncMock()
        controls = {}

        def locator(selector):
            if selector not in controls:
                c = Mock(
                    click=AsyncMock(),
                    fill=AsyncMock(),
                    check=AsyncMock(),
                    count=AsyncMock(return_value=1),
                    is_visible=AsyncMock(return_value=True),
                    inner_text=AsyncMock(return_value="Friend (friend@smu.edu.sg)"),
                    wait_for=AsyncMock(),
                )
                c.first = c
                c.locator.return_value = c
                c.get_by_text.return_value = c
                c.filter.return_value = c
                controls[selector] = c
            return controls[selector]

        frame = Mock()
        frame.locator.side_effect = locator
        field = locator(
            "input#bookingFormControl1_DialogSearchCoBooker_searchPanel_textBox_c1"
        )
        field.is_visible.return_value = False
        watermark = locator(
            "#bookingFormControl1_DialogSearchCoBooker_searchPanel_textBox input:visible"
        )
        order = Mock()
        order.attach_mock(watermark.click, "focus")
        order.attach_mock(field.fill, "fill")
        await client._add_co_booker(frame, "friend@smu.edu.sg")
        self.assertEqual(
            order.mock_calls,
            [call.focus(timeout=15000), call.fill("friend@smu.edu.sg")],
        )
        grid = controls["table#bookingFormControl1_GridCoBookers_gv:visible"]
        self.assertTrue(
            grid.filter.call_args.kwargs["has_text"].search("FRIEND@SMU.EDU.SG")
        )


class SelectionRetentionTest(unittest.IsolatedAsyncioTestCase):
    async def test_requires_two_stable_exact_selection_snapshots(self):
        client = FBSClient()
        right = [
            {
                "room": "Room A",
                "start": "2026-11-02T14:00:00",
                "end": "2026-11-02T15:00:00",
            }
        ]
        client._selection_snapshot = AsyncMock(
            side_effect=[[], right, [], right, right]
        )
        frame = Mock(wait_for_timeout=AsyncMock())
        await client._verify_calendar_selection(
            frame, "Room A", "14:00", "15:00", "02-Nov-2026"
        )
        self.assertEqual(client._selection_snapshot.await_count, 5)

    async def test_empty_wrong_room_date_time_or_multiple_ranges_are_rejected(self):
        right = {
            "room": "Room A",
            "start": "2026-11-02T14:00:00",
            "end": "2026-11-02T15:00:00",
        }
        for ranges in [
            [],
            [dict(right, room="Room B")],
            [dict(right, start="2026-11-03T14:00:00", end="2026-11-03T15:00:00")],
            [dict(right, end="2026-11-02T15:30:00")],
            [right, right],
        ]:
            client = FBSClient()
            client._selection_snapshot = AsyncMock(return_value=ranges)
            with self.subTest(ranges=ranges), self.assertRaises(CalendarSelectionLost):
                await client._verify_calendar_selection(
                    Mock(wait_for_timeout=AsyncMock()),
                    "Room A",
                    "14:00",
                    "15:00",
                    "02-Nov-2026",
                )

    async def test_lost_selection_is_retried_once_before_make_booking(self):
        client = FBSClient(progress=AsyncMock())
        client._drag_timeslot = AsyncMock(
            side_effect=[CalendarSelectionLost("lost"), None]
        )
        client._verify_calendar_selection = AsyncMock()
        client._wait_for_idle = AsyncMock()
        button = Mock(hover=AsyncMock(), click=AsyncMock())
        clear = Mock(count=AsyncMock(return_value=1), click=AsyncMock())
        clear.first = clear
        frame = Mock()
        frame.locator.return_value = button
        button.get_by_text.return_value.all = AsyncMock(return_value=[])
        frame.get_by_text.return_value = clear
        details = Mock()
        details.locator.return_value.is_visible = AsyncMock(return_value=True)
        client.page = Mock()
        client.page.frame.side_effect = lambda name: (
            details if name == "frameBookingDetails" else frame
        )
        request = BookingRequest(
            SearchConfig("02-Nov-2026", "14:00", "15:00"),
            "Room A",
            "Meeting",
            "friend@smu.edu.sg",
        )
        self.assertIs(await client._prepare_booking_frame(frame, request), details)
        self.assertEqual(client._drag_timeslot.await_count, 2)
        button.click.assert_awaited_once()
        clear.click.assert_awaited_once()

    async def test_second_failed_drag_does_not_click_make_booking(self):
        client = FBSClient(progress=AsyncMock())
        client._drag_timeslot = AsyncMock(side_effect=CalendarSelectionLost("lost"))
        client._wait_for_idle = AsyncMock()
        frame = Mock()
        frame.get_by_text.return_value.count = AsyncMock(return_value=0)
        frame.locator.return_value.click = AsyncMock()
        frame.locator.return_value.get_by_text.return_value.all = AsyncMock(
            return_value=[]
        )
        client.page = Mock()
        client.page.frame.return_value = frame
        request = BookingRequest(
            SearchConfig("02-Nov-2026", "14:00", "15:00"),
            "Room A",
            "Meeting",
            "friend@smu.edu.sg",
        )
        with self.assertRaises(CalendarSelectionLost):
            await client._prepare_booking_frame(frame, request)
        self.assertEqual(client._drag_timeslot.await_count, 2)
        frame.locator.return_value.click.assert_not_awaited()

    async def test_visible_calendar_rejection_is_reported_without_waiting_for_missing_frame(
        self,
    ):
        client = FBSClient(progress=AsyncMock())
        client._drag_timeslot = AsyncMock()
        client._verify_calendar_selection = AsyncMock()
        client._wait_for_idle = AsyncMock()
        frame = Mock()
        button = Mock(hover=AsyncMock(), click=AsyncMock())
        error = Mock(
            all_text_contents=AsyncMock(
                return_value=["DISMISS Click and drag to select timeslot"]
            )
        )
        dismiss = Mock(click=AsyncMock())
        error.get_by_text.return_value.all = AsyncMock(return_value=[dismiss])
        frame.locator.side_effect = lambda selector: (
            error if selector == ".error:visible" else button
        )
        frame.get_by_text.return_value.count = AsyncMock(return_value=0)
        client.page = Mock(wait_for_timeout=AsyncMock())
        client.page.frame.side_effect = lambda name: (
            None if name == "frameBookingDetails" else frame
        )
        request = BookingRequest(
            SearchConfig("02-Nov-2026", "14:00", "15:00"),
            "Room A",
            "Meeting",
            "friend@smu.edu.sg",
        )
        with self.assertRaisesRegex(CalendarSelectionLost, "rejected"):
            await client._prepare_booking_frame(frame, request)
        client.page.wait_for_timeout.assert_not_awaited()
        self.assertEqual(dismiss.click.await_count, 2)
