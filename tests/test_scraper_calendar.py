import unittest

from bot.fbs_client import LayoutChanged
from bot.scraper_calendar import calendar_rooms
from bot.snapshot import search_days


class CalendarCoverageTest(unittest.TestCase):
    def setUp(self):
        self.config = search_days()[0]
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
