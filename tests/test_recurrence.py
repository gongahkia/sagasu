"""Bounded SGT series expansion and per-date availability outcomes."""

import unittest
from unittest.mock import AsyncMock

from mcp import Client
from sagasu.mcp_service import AgentService, Backend
from sagasu.mcp_server import create_server
from sagasu.room_plans import find_room_plans, prepare_plan, recurrence_dates
from sagasu.fbs_client import LayoutChanged
from tests.test_room_plans import CONFIG, snapshot, room


class RecurrenceDatesTests(unittest.TestCase):
    def test_inclusive_mondays_and_biweekly_ranges_anchor_to_start_week(self):
        self.assertEqual(
            recurrence_dates(
                {"start_date": "2026-11-15", "end_date": "2026-11-30", "weekdays": [0]}
            ),
            ["16-Nov-2026", "23-Nov-2026", "30-Nov-2026"],
        )
        self.assertEqual(
            recurrence_dates(
                {
                    "start_date": "2026-11-18",
                    "end_date": "2026-12-10",
                    "weekdays": [0, 2],
                    "interval_weeks": 2,
                }
            ),
            ["18-Nov-2026", "30-Nov-2026", "02-Dec-2026"],
        )

    def test_leap_days_and_year_boundaries(self):
        self.assertEqual(
            recurrence_dates(
                {"start_date": "2028-02-29", "end_date": "2028-02-29", "weekdays": [1]}
            ),
            ["29-Feb-2028"],
        )
        self.assertEqual(
            recurrence_dates(
                {"start_date": "2026-12-28", "end_date": "2027-01-04", "weekdays": [0]}
            ),
            ["28-Dec-2026", "04-Jan-2027"],
        )

    def test_invalid_or_unbounded_series_fail_without_silent_truncation(self):
        base = {"start_date": "2099-11-01", "end_date": "2099-11-30", "weekdays": [0]}
        for value in [
            None,
            {},
            {**base, "weekdays": [True]},
            {**base, "weekdays": [7]},
            {**base, "weekdays": [0, 0]},
            {**base, "weekdays": []},
            {**base, "interval_weeks": 0},
            {**base, "end_date": "2099-10-31"},
            {**base, "end_date": "2101-01-01"},
            {**base, "weekdays": list(range(7)), "end_date": "2099-12-02"},
            {**base, "start_date": 9},
            {**base, "extra": True},
            {**base, "end_date": "2099-11-01", "weekdays": [0]},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                recurrence_dates(value)
        with self.assertRaises(ValueError):
            prepare_plan(CONFIG, dates=[CONFIG["date"]], recurrence=base)


class RecurrenceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_date_keeps_success_unavailability_and_failure_distinct(self):
        recurrence = {
            "start_date": "2099-11-02",
            "end_date": "2099-11-16",
            "weekdays": [0],
        }

        def read(config):
            if config.date == "16-Nov-2099":
                raise LayoutChanged("PRIVATE FAILURE")
            return snapshot(
                config,
                [
                    room(
                        "A",
                        [
                            (
                                "12:00",
                                "16:00",
                                "booked" if config.date == "09-Nov-2099" else "free",
                            )
                        ],
                    )
                ],
            )

        source = AsyncMock(side_effect=read)
        plan = await find_room_plans(
            prepare_plan(CONFIG, recurrence=recurrence), source
        )
        self.assertEqual(source.await_count, 3)
        self.assertEqual(
            [p["status"] for p in plan["probes"]], ["succeeded", "succeeded", "failed"]
        )
        self.assertTrue(plan["probes"][0]["matching_plan_available"])
        self.assertFalse(plan["probes"][1]["matching_plan_available"])
        self.assertNotIn("matching_plan_available", plan["probes"][2])
        self.assertTrue(plan["partial_failure"])
        self.assertFalse(plan["submitted"])

    async def test_mcp_generates_explicit_dates_and_keeps_each_occurrence_reviewable(
        self,
    ):
        backend = Backend(interactive_auth=False)
        backend.reads.mode = "api"
        backend.reads.direct = AsyncMock(
            side_effect=lambda kind, config, progress: snapshot(config)
        )
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        async with Client(create_server(service)) as client:
            response = await client.call_tool(
                "plan_rooms",
                {
                    "config": CONFIG,
                    "duration_minutes": 60,
                    "recurrence": {
                        "start_date": "2099-11-02",
                        "end_date": "2099-11-16",
                        "weekdays": [0],
                    },
                    "mode": "common",
                },
            )
            self.assertFalse(response.is_error)
            operation = service.get(response.structured_content["operation_id"])
            await operation.task
            self.assertEqual(
                operation.result["dates"], ["02-Nov-2099", "09-Nov-2099", "16-Nov-2099"]
            )
            review = service.planned_booking_review(
                operation.id,
                0,
                occurrence_index=2,
                purpose="Study",
                co_booker="friend@smu.edu.sg",
            )
            self.assertEqual(review["summary"]["search"]["date"], "16-Nov-2099")
        self.assertEqual(backend.reads.direct.await_count, 3)
        self.assertEqual(len(service.reviews), 1)
