import unittest

from bot.fbs_client import (
    BookingRequest,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
    add_free_windows,
    minutes_to_time,
    parse_event_title,
    time_to_minutes,
)


class TimeHelpersTest(unittest.TestCase):
    def test_time_round_trip(self):
        self.assertEqual(time_to_minutes("09:30"), 570)
        self.assertEqual(minutes_to_time(570), "09:30")

    def test_invalid_time_is_rejected(self):
        for value in ("9:30", "10:60", "24:00", "nope"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                time_to_minutes(value)

    def test_parse_booked_and_unavailable_events(self):
        booked = parse_event_title("Booking Time: 09:00-10:30\nStatus: Confirmed")
        unavailable = parse_event_title("(12:00-13:00) (not available)")
        self.assertEqual((booked.start, booked.end, booked.status), ("09:00", "10:30", "booked"))
        self.assertEqual(unavailable.status, "unavailable")
        self.assertIsNone(parse_event_title("unexpected"))

    def test_free_complement_clamps_and_merges_overlap(self):
        blocked = [
            TimeSlot("08:00", "09:30", "booked"),
            TimeSlot("09:00", "10:00", "unavailable"),
            TimeSlot("11:00", "12:00", "booked"),
        ]
        result = add_free_windows(blocked, "09:00", "12:30")
        self.assertEqual(
            [(slot.start, slot.end, slot.status) for slot in result],
            [
                ("09:00", "09:30", "booked"),
                ("09:30", "10:00", "unavailable"),
                ("10:00", "11:00", "free"),
                ("11:00", "12:00", "booked"),
                ("12:00", "12:30", "free"),
            ],
        )


class ContractsTest(unittest.TestCase):
    def setUp(self):
        self.search = SearchConfig(
            date="12-Oct-2026",
            start_time="09:00",
            end_time="12:00",
            buildings=("School of Computing & Information Systems 1",),
            floors=("Level 2",),
            facility_types=("Group Study Room",),
        )

    def test_search_config_round_trip(self):
        self.assertEqual(SearchConfig.from_mapping(self.search.__dict__), self.search)

    def test_config_rejects_invalid_window(self):
        with self.assertRaises(ValueError):
            SearchConfig("12-Oct-2026", "12:00", "09:00")
        with self.assertRaises(ValueError):
            SearchConfig("12-Oct-2026", "09:15", "10:00")

    def test_booking_request_round_trip(self):
        request = BookingRequest(
            search=self.search,
            room="SCIS1 GSR 2-1",
            purpose="Project meeting",
            co_booker="friend@smu.edu.sg",
        )
        self.assertEqual(BookingRequest.from_mapping(request.to_dict()), request)

    def test_bookable_rooms_are_ranked_deterministically(self):
        rooms = [
            RoomAvailability("B", [TimeSlot("09:00", "12:00", "free")]),
            RoomAvailability("A", [TimeSlot("09:00", "12:30", "free")]),
            RoomAvailability("C", [TimeSlot("10:00", "12:00", "free")]),
        ]
        result = SearchResult(self.search, rooms, "2026-10-09T12:00:00+08:00")
        self.assertEqual([room.name for room in result.bookable_rooms], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
