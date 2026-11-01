import unittest

from bot.fbs_client import LayoutChanged, SearchConfig
from bot.fbs_calendar import calendar_rooms


class CalendarCoverageTest(unittest.TestCase):
    def setUp(self):
        self.config = SearchConfig("01-Nov-2026", "08:00", "22:00")
        self.rows = {"A": (0, 25), "B": (300, 325)}
        self.event = {
            "title": "Booking Time: 10:00-11:00\nPRIVATE PERSON",
            "top": 301,
            "bottom": 324,
            "height": 23,
        }

    def test_offscreen_event_is_assigned_to_its_own_room_without_details(self):
        rooms = calendar_rooms(["A", "B"], self.config, self.rows, [self.event], 1)
        self.assertTrue(rooms[0].is_free("08:00", "22:00"))
        self.assertFalse(rooms[1].is_free("10:00", "11:00"))
        self.assertTrue(rooms[1].is_free("11:00", "22:00"))
        self.assertTrue(
            all(slot.details is None for room in rooms for slot in room.timeslots)
        )

    def test_midnight_to_next_midnight_unavailable_is_not_advertised_as_free(self):
        event = {
            **self.event,
            "title": "(00:00-00:00) (not available)",
            "start_date": "2026-11-01T00:00:00",
            "end_date": "2026-11-02T00:00:00",
        }
        rooms = calendar_rooms(["A", "B"], self.config, self.rows, [event], 1)
        self.assertFalse(rooms[1].is_free("08:00", "22:00"))
        self.assertEqual(
            [(slot.start, slot.end, slot.status) for slot in rooms[1].timeslots],
            [("08:00", "22:00", "unavailable")],
        )

    def test_previous_day_booking_only_blocks_its_overlap_with_requested_date(self):
        event = {
            **self.event,
            "title": "Booking Time: 23:00-09:00",
            "start_date": "2026-10-31T23:00:00",
            "end_date": "2026-11-01T09:00:00",
        }
        rooms = calendar_rooms(["A", "B"], self.config, self.rows, [event], 1)
        self.assertFalse(rooms[1].is_free("08:00", "09:00"))
        self.assertTrue(rooms[1].is_free("09:00", "22:00"))

    def test_model_resource_mismatch_is_rejected(self):
        event = {**self.event, "resource_name": "A"}
        with self.assertRaises(LayoutChanged):
            calendar_rooms(["A", "B"], self.config, self.rows, [event], 1)

    def test_missing_or_nonpositive_full_dates_are_rejected(self):
        for start, end in [
            (None, None),
            ("invalid", "invalid"),
            ("2026-11-01T00:00:00", "2026-11-01T00:00:00"),
        ]:
            event = {**self.event, "start_date": start, "end_date": end}
            with self.assertRaises(LayoutChanged):
                calendar_rooms(["A", "B"], self.config, self.rows, [event], 1)

    def test_second_event_line_in_expanded_room_row_stays_busy(self):
        rows = {"A": (0, 25), "B": (300, 350)}
        second = {
            "title": "Booking Time: 10:30-12:00",
            "top": 326,
            "bottom": 349,
            "height": 23,
        }
        rooms = calendar_rooms(["A", "B"], self.config, rows, [self.event, second], 2)
        self.assertFalse(rooms[1].is_free("11:00", "12:00"))
        self.assertTrue(rooms[1].is_free("12:00", "22:00"))

    def test_missing_room_row_is_rejected_even_if_no_events(self):
        with self.assertRaises(LayoutChanged):
            calendar_rooms(["A", "B"], self.config, {"A": self.rows["A"]}, [], 0)

    def test_missing_event_is_rejected_before_advertising_free_rooms(self):
        with self.assertRaises(LayoutChanged):
            calendar_rooms(["A", "B"], self.config, self.rows, [], 1)

    def test_unreadable_event_is_rejected(self):
        with self.assertRaises(LayoutChanged):
            calendar_rooms(
                ["A", "B"],
                self.config,
                self.rows,
                [{**self.event, "title": "unknown PRIVATE"}],
                1,
            )

    def test_event_in_a_group_row_is_not_assigned_to_nearest_room(self):
        with self.assertRaises(LayoutChanged):
            calendar_rooms(
                ["A", "B"],
                self.config,
                self.rows,
                [{**self.event, "top": 200, "bottom": 223}],
                1,
            )

    def test_overlapping_room_geometry_is_rejected(self):
        with self.assertRaises(LayoutChanged):
            calendar_rooms(
                ["A", "B"],
                self.config,
                {"A": (300, 325), "B": (300, 325)},
                [self.event],
                1,
            )

    def test_no_bookings_with_all_room_rows_is_valid(self):
        rooms = calendar_rooms(["A", "B"], self.config, self.rows, [], 0)
        self.assertTrue(all(room.is_free("08:00", "22:00") for room in rooms))
