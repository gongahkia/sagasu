import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sagasu.fbs_client import (
    FBS_TIMEZONE,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
    search_config_from_preferences,
)


class RollingSearchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 1, 13, 24, tzinfo=FBS_TIMEZONE)
        clock = patch("sagasu.fbs_client.datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.side_effect = lambda tz=None: self.now.astimezone(tz)
        self.addCleanup(clock.stop)

    def test_default_is_next_half_hour_for_two_hours_without_persisting_times(self):
        config = {}
        result = search_config_from_preferences(config)
        self.assertEqual(
            (result.date, result.start_time, result.end_time),
            ("01-Nov-2026", "13:30", "15:30"),
        )
        self.assertEqual(config, {})
        self.now = self.now.replace(minute=31)
        result = search_config_from_preferences(config)
        self.assertEqual((result.start_time, result.end_time), ("14:00", "16:00"))

    def test_exact_boundary_uses_next_future_half_hour(self):
        self.now = self.now.replace(minute=30, second=0)
        self.assertEqual(search_config_from_preferences({}).start_time, "14:00")

    def test_late_window_advances_to_next_date_without_shortening_duration(self):
        self.now = self.now.replace(hour=22, minute=1)
        config = search_config_from_preferences({})
        self.assertEqual(
            (config.date, config.start_time, config.end_time),
            ("02-Nov-2026", "00:00", "02:00"),
        )

    def test_explicit_choices_are_preserved_and_duration_can_override_default(self):
        config = {
            "date_raw": "2 November 2026",
            "start_time": "09:30",
            "duration_hrs": 1.5,
        }
        result = search_config_from_preferences(config)
        self.assertEqual(
            (result.date, result.start_time, result.end_time),
            ("02-Nov-2026", "09:30", "11:00"),
        )
        self.now = self.now.replace(hour=18)
        self.assertEqual(search_config_from_preferences(config), result)
