"""Explicit FBS horizon restrictions remain distinct from occupied rooms."""

import unittest
from unittest.mock import AsyncMock, patch

from sagasu.fbs_calendar import calendar_rooms
from sagasu.fbs_client import (
    BookingRequest,
    BookingUnavailable,
    FBSClient,
    LayoutChanged,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
    FBSError,
)
from tests.test_mcp import CONFIG, ServiceCase


class HorizonCalendarTests(unittest.TestCase):
    def setUp(self):
        self.config = SearchConfig("23-Nov-2026", "11:00", "14:00")
        self.event = {
            "title": "(00:00-00:00) (not within booking horizon)",
            "top": 1,
            "bottom": 24,
            "height": 23,
            "resource_name": "A",
            "start_date": "2026-11-23T00:00:00",
            "end_date": "2026-11-24T00:00:00",
        }

    def result(self, events):
        rooms = calendar_rooms(
            ["A", "B"], self.config, {"A": (0, 25), "B": (25, 50)}, events, len(events)
        )
        return SearchResult(self.config, rooms, "test")

    def test_all_rooms_outside_horizon_are_blocked_with_explicit_diagnosis(self):
        second = {**self.event, "resource_name": "B", "top": 26, "bottom": 49}
        result = self.result([self.event, second])
        self.assertEqual(result.bookable_rooms, [])
        diagnosis = result.to_dict()["booking_horizon"]
        self.assertTrue(diagnosis["all_checked_rooms_affected"])
        self.assertEqual(diagnosis["checked_rooms"], 2)
        self.assertEqual(diagnosis["affected_rooms"], ["A", "B"])

    def test_different_room_horizon_does_not_hide_available_alternative(self):
        result = self.result([self.event])
        self.assertEqual([r.name for r in result.bookable_rooms], ["B"])
        self.assertFalse(result.booking_horizon["all_checked_rooms_affected"])

    def test_horizon_on_different_date_does_not_block_requested_date(self):
        event = {
            **self.event,
            "start_date": "2026-11-24T00:00:00",
            "end_date": "2026-11-25T00:00:00",
        }
        result = self.result([event])
        self.assertIsNone(result.booking_horizon)
        self.assertEqual(len(result.bookable_rooms), 2)

    def test_occupied_alternative_does_not_imply_shared_horizon(self):
        occupied = {
            **self.event,
            "resource_name": "B",
            "top": 26,
            "bottom": 49,
            "title": "Booking Time: 11:00-14:00",
        }
        result = self.result([self.event, occupied])
        self.assertEqual(result.bookable_rooms, [])
        self.assertFalse(result.booking_horizon["all_checked_rooms_affected"])
        self.assertEqual(result.booking_horizon["affected_rooms"], ["A"])

    def test_ordinary_unavailability_has_no_horizon_diagnosis(self):
        event = {**self.event, "title": "(00:00-00:00) (not available)"}
        result = self.result([event])
        self.assertIsNone(result.booking_horizon)

    def test_horizon_without_dated_model_still_fails_closed(self):
        event = {
            k: v for k, v in self.event.items() if k not in {"start_date", "end_date"}
        }
        with self.assertRaises(LayoutChanged):
            self.result([event])


class HorizonBookingTests(unittest.IsolatedAsyncioTestCase):
    @patch("sagasu.fbs_client.require_future_booking")
    async def test_failed_alternative_search_preserves_known_reason(self, future):
        config = SearchConfig("23-Nov-2026", "11:00", "14:00")
        blocked = RoomAvailability(
            "A", [TimeSlot("11:00", "14:00", "outside_booking_horizon")]
        )
        client = FBSClient(progress=AsyncMock())
        client.search = AsyncMock(
            side_effect=[
                SearchResult(config, [blocked], "test"),
                FBSError("Read failed"),
            ]
        )
        with self.assertRaisesRegex(
            BookingUnavailable, "shared booking-horizon restriction is unverified"
        ):
            await client.book(
                BookingRequest(config, "A", "Meeting", "friend@smu.edu.sg"), submit=True
            )

    @patch("sagasu.fbs_client.require_future_booking")
    async def test_rechecks_alternatives_at_same_time_without_submitting(self, future):
        config = SearchConfig(
            "23-Nov-2026", "11:00", "14:00", buildings=("Li Ka Shing Library",)
        )
        blocked = RoomAvailability(
            "A", [TimeSlot("11:00", "14:00", "outside_booking_horizon")]
        )
        free = RoomAvailability("B", [TimeSlot("11:00", "14:00", "free")])
        for rooms, expected in [
            ([blocked, free], "1 alternatives"),
            ([blocked], "too far ahead"),
        ]:
            with self.subTest(expected=expected):
                client = FBSClient(progress=AsyncMock())
                client.search = AsyncMock(
                    side_effect=[
                        SearchResult(config, [blocked], "test"),
                        SearchResult(config, rooms, "test"),
                    ]
                )
                with self.assertRaisesRegex(BookingUnavailable, expected):
                    await client.book(
                        BookingRequest(config, "A", "Meeting", "friend@smu.edu.sg"),
                        submit=True,
                    )
                self.assertEqual(
                    client.search.await_args_list[0].kwargs, {"room_name": "A"}
                )
                self.assertEqual(client.search.await_args_list[1].args, (config,))
                self.assertEqual(client.search.await_args_list[1].kwargs, {})


class HorizonMCPTests(ServiceCase):
    async def test_available_rooms_keeps_horizon_diagnosis_before_pagination(self):
        job = await self.finish(self.service.search(CONFIG))
        job.result["rooms"] = [
            {
                "name": name,
                "timeslots": [
                    {
                        "start": "14:00",
                        "end": "16:00",
                        "status": "outside_booking_horizon",
                    }
                ],
            }
            for name in ["A", "B"]
        ]
        result = self.service.available_rooms(job.id, limit=1)
        self.assertEqual(result["available_total"], 0)
        self.assertEqual(result["booking_horizon"]["checked_rooms"], 2)
        self.assertTrue(result["booking_horizon"]["all_checked_rooms_affected"])
