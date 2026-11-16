"""Clash checks distinguish verified overlaps from incomplete account evidence."""

import copy
import unittest
from unittest.mock import AsyncMock, patch

from mcp import Client
from sagasu.clashes import check_clashes, prepare_windows
from sagasu.fbs_client import LayoutChanged
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentError, AgentService, Backend
from tests.test_mcp import ROW


WINDOW = {"date": "01-Nov-2099", "start_time": "14:00", "end_time": "16:00"}


def listing(*statuses):
    rows = [
        {**copy.deepcopy(ROW), "reference_number": f"BK-{i}", "fbs_status": status}
        for i, status in enumerate(statuses)
    ]
    return {"bookings": rows, "total": len(rows), "complete": True}


class ClashTests(unittest.TestCase):
    def test_outer_window_reports_no_two_hour_continuous_gap(self):
        source = listing("Confirmed")
        source["bookings"][0]["date_time"] = "01-Nov-2099 (Sun) 10:30 - 14:30 (4hrs)"
        result = check_clashes(
            source, [{**WINDOW, "start_time": "10:00", "end_time": "16:00"}]
        )["windows"][0]
        self.assertTrue(result["complete"])
        self.assertEqual(
            result["clear_intervals"],
            [
                {"start_time": "10:00", "end_time": "10:30", "duration_minutes": 30},
                {"start_time": "14:30", "end_time": "16:00", "duration_minutes": 90},
            ],
        )
        self.assertEqual(result["longest_clear_minutes"], 90)

    def test_clear_intervals_merge_overlaps_clip_bounds_and_include_unpaged_rows(self):
        source = listing(*["Confirmed"] * 51)
        for row in source["bookings"]:
            row["date_time"] = "01-Nov-2099 (Sun) 13:30 - 14:30 (1hr)"
        source["bookings"][-1]["date_time"] = "01-Nov-2099 (Sun) 14:30 - 16:30 (2hrs)"
        result = check_clashes(source, [WINDOW])["windows"][0]
        self.assertTrue(result["overlaps_truncated"])
        self.assertEqual(result["clear_intervals"], [])
        self.assertEqual(result["longest_clear_minutes"], 0)
        source["bookings"] = source["bookings"][:1]
        source["total"] = 1
        source["bookings"][0]["date_time"] = "01-Nov-2099 (Sun) 15:30 - 16:30 (1hr)"
        self.assertEqual(
            check_clashes(source, [WINDOW])["windows"][0]["longest_clear_minutes"], 90
        )

    def test_confirmed_pending_and_cancelled_rows_have_distinct_meanings(self):
        result = check_clashes(
            listing(
                "Confirmed",
                "Pending Confirmation",
                "Cancelled",
                "Withdrawn",
                "Rejected",
            ),
            [WINDOW],
        )
        self.assertTrue(result["complete"])
        check = result["windows"][0]
        self.assertFalse(check["clear_of_existing_bookings"])
        self.assertEqual(
            [row["classification"] for row in check["overlaps"]],
            ["confirmed", "pending"],
        )
        self.assertFalse(result["submitted"])

    def test_adjacency_different_days_and_proposed_intersections_use_half_open_intervals(
        self,
    ):
        windows = [
            {**WINDOW, "start_time": "12:00", "end_time": "14:00"},
            {**WINDOW, "start_time": "16:00", "end_time": "17:00"},
            {**WINDOW, "date": "02-Nov-2099"},
            {**WINDOW, "start_time": "16:30", "end_time": "17:30"},
        ]
        result = check_clashes(listing("Confirmed"), windows)
        self.assertTrue(
            all(row["clear_of_existing_bookings"] for row in result["windows"])
        )
        self.assertEqual(result["proposed_overlaps"], [[1, 3]])

    def test_partial_coverage_or_unreadable_active_window_never_produces_an_all_clear(
        self,
    ):
        partial = {**listing(), "total": 4, "complete": False}
        unreadable = listing("Confirmed")
        unreadable["bookings"][0]["date_time"] = "Unknown date/time"
        for source in [partial, unreadable]:
            with self.subTest(source=source):
                result = check_clashes(source, [WINDOW])
                self.assertFalse(result["complete"])
                self.assertIsNone(result["windows"][0]["clear_of_existing_bookings"])
                self.assertIsNone(result["windows"][0]["clear_intervals"])
                self.assertIsNone(result["windows"][0]["longest_clear_minutes"])
        cancelled = listing("Cancelled")
        cancelled["bookings"][0]["date_time"] = "Unknown date/time"
        self.assertTrue(
            check_clashes(cancelled, [WINDOW])["windows"][0][
                "clear_of_existing_bookings"
            ]
        )

    def test_unknown_overlapping_status_remains_uncertain_and_is_not_discarded(self):
        result = check_clashes(listing("Unknown native status"), [WINDOW])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["windows"][0]["clear_of_existing_bookings"])
        self.assertIsNone(result["windows"][0]["clear_intervals"])
        self.assertEqual(result["windows"][0]["overlap_count"], 1)
        self.assertEqual(
            result["windows"][0]["overlaps"][0]["classification"], "unknown_status"
        )

    def test_duplicate_references_and_invalid_totals_fail_closed(self):
        source = listing("Confirmed", "Confirmed")
        source["bookings"][1]["reference_number"] = source["bookings"][0][
            "reference_number"
        ]
        for invalid in [source, {**listing("Confirmed"), "total": 0}]:
            with self.subTest(invalid=invalid), self.assertRaises(LayoutChanged):
                check_clashes(invalid, [WINDOW])

    def test_output_bounds_do_not_hide_the_actual_overlap_count(self):
        result = check_clashes(listing(*["Confirmed"] * 51), [WINDOW])
        check = result["windows"][0]
        self.assertEqual(check["overlap_count"], 51)
        self.assertEqual(len(check["overlaps"]), 50)
        self.assertTrue(check["overlaps_truncated"])
        self.assertFalse(check["clear_of_existing_bookings"])

    def test_invalid_proposals_fail_before_any_account_read(self):
        self.assertEqual(prepare_windows([{**WINDOW, "date": "2099-11-01"}]), [WINDOW])
        for windows in [
            [],
            [WINDOW] * 32,
            [None],
            [{**WINDOW, "date": "2020-01-01"}],
            [{**WINDOW, "room": "Room A"}],
            [{**WINDOW, "start_time": "14:15"}],
        ]:
            with (
                self.subTest(windows=windows),
                self.assertRaises((ValueError, TypeError, RuntimeError)),
            ):
                prepare_windows(windows)


class ClashWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_checks_all_proposals_using_one_hybrid_account_read_without_a_browser_or_write(
        self,
    ):
        backend = Backend(interactive_auth=False)
        backend.reads.mode = "api"
        backend.reads.direct = AsyncMock(return_value=listing("Pending Confirmation"))
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        with patch("sagasu.mcp_service.FBSClient") as browser:
            async with Client(create_server(service)) as client:
                response = await client.call_tool(
                    "check_booking_clashes",
                    {"windows": [WINDOW, {**WINDOW, "date": "2099-11-02"}]},
                )
                self.assertFalse(response.is_error)
                operation = service.get(response.structured_content["operation_id"])
                await operation.task
                self.assertEqual(operation.status, "succeeded")
                self.assertFalse(
                    operation.result["windows"][0]["clear_of_existing_bookings"]
                )
                self.assertTrue(
                    operation.result["windows"][1]["clear_of_existing_bookings"]
                )
        browser.assert_not_called()
        backend.reads.direct.assert_awaited_once()
        self.assertEqual(backend.reads.direct.await_args.args[0], "bookings")
        self.assertEqual(service.reviews, {})
        self.assertIsNone(service.journal)
        with self.assertRaises(AgentError):
            service.check_booking_clashes([])
