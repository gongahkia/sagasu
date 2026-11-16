"""Upcoming selection is time-aware, coverage-aware and independent of paging."""

import copy
import unittest
from datetime import datetime, timezone

from mcp import Client

from sagasu.booking_timeline import annotate_booking_timeline
from sagasu.fbs_client import FBS_TIMEZONE
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService
from tests.test_mcp import FakeBackend


NOW = datetime(2026, 11, 16, 14, 54, tzinfo=FBS_TIMEZONE)


def row(reference, date="16-Nov-2026", start="14:30", end="17:30", status="Confirmed"):
    return {
        "reference_number": reference,
        "date_time": f"{date} (Mon) {start} - {end} (3hrs)",
        "room": "Test room",
        "fbs_status": status,
    }


def listing(*rows):
    return {"bookings": list(rows), "total": len(rows), "complete": True}


class BookingTimelineTests(unittest.TestCase):
    def test_ongoing_is_separate_and_next_uses_sgt_start_order_without_mutation(self):
        data = listing(
            row("ongoing"),
            row("later", date="18-Nov-2026"),
            row("next", date="17-Nov-2026"),
            row("cancelled", start="15:00", end="16:00", status="Cancelled"),
            row("ended", start="11:00", end="14:00"),
        )
        original = copy.deepcopy(data)
        result = annotate_booking_timeline(data, now=NOW.astimezone(timezone.utc))
        self.assertEqual(result["timeline"]["next_upcoming_reference"], "next")
        self.assertEqual(result["timeline"]["ongoing_references"], ["ongoing"])
        self.assertEqual(result["bookings"][0]["temporal_status"], "ongoing")
        self.assertEqual(
            result["bookings"][0]["starts_at"], "2026-11-16T14:30:00+08:00"
        )
        self.assertTrue(result["timeline"]["complete"])
        self.assertEqual(data, original)

    def test_exact_start_is_ongoing_and_exact_end_is_past(self):
        result = annotate_booking_timeline(
            listing(
                row("starting", start="14:30", end="16:30"),
                row("ending", start="12:30", end="14:30"),
            ),
            now=NOW.replace(hour=14, minute=30),
        )
        self.assertEqual(
            [row["temporal_status"] for row in result["bookings"]], ["ongoing", "past"]
        )
        self.assertEqual(result["timeline"]["next_upcoming_status"], "none")
        self.assertEqual(result["timeline"]["ongoing_references"], ["starting"])

    def test_partial_or_ambiguous_listing_cannot_claim_next_or_absence(self):
        future = row("next", date="17-Nov-2026")
        invalid = [
            {**listing(future), "complete": False},
            {**listing(future), "total": 2},
            listing(future, {**row("unreadable"), "date_time": "unknown"}),
            listing(future, row("unknown-status", status="UNRECOGNISED")),
            listing(future, future),
        ]
        for data in invalid:
            with self.subTest(data=data):
                result = annotate_booking_timeline(data, now=NOW)
                self.assertFalse(result["timeline"]["complete"])
                self.assertIsNone(result["timeline"]["next_upcoming_reference"])
                self.assertEqual(result["timeline"]["next_upcoming_status"], "unknown")

    def test_cancelled_unreadable_rows_do_not_hide_known_pending_next(self):
        result = annotate_booking_timeline(
            listing(
                {**row("cancelled", status="Cancelled"), "date_time": "unknown"},
                row("pending", date="17-Nov-2026", status="Pending Confirmation"),
            ),
            now=NOW,
        )
        self.assertTrue(result["timeline"]["complete"])
        self.assertEqual(result["timeline"]["next_upcoming_reference"], "pending")
        self.assertEqual(result["bookings"][1]["fbs_status"], "Pending Confirmation")


class BookingTimelineProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_pointer_uses_full_listing_and_reclassifies_cached_rows(self):
        records = listing(row("ongoing", end="15:00"), row("next", date="17-Nov-2026"))

        class Backend(FakeBackend):
            async def run(self, kind, payload, progress, notify):
                self.calls.append(kind)
                return copy.deepcopy(records)

        backend = Backend()
        clock = [NOW.timestamp()]
        service = AgentService(backend=backend, clock=lambda: clock[0])
        self.addAsyncCleanup(service.close)
        async with Client(create_server(service)) as client:
            started = (await client.call_tool("list_bookings", {})).structured_content
            completed = (
                await client.call_tool(
                    "wait_operation",
                    {"operation_id": started["operation_id"], "limit": 1},
                )
            ).structured_content
            self.assertEqual(completed["status"], "succeeded")
            result = completed["result"]
            self.assertEqual(len(result["bookings"]), 1)
            self.assertEqual(result["bookings"][0]["reference_number"], "ongoing")
            self.assertEqual(result["timeline"]["next_upcoming_reference"], "next")
            self.assertEqual(result["pagination"]["next_offset"], 1)
            self.assertEqual(result["pagination"]["returned_count"], 1)
            self.assertEqual(result["pagination"]["retained_total"], 2)
            self.assertEqual(result["pagination"]["source_total"], 2)
            # Selection uses the request-time clock, while FBS status remains
            # from this listing. No second FBS read or action is triggered.
            clock[0] = NOW.replace(hour=15, minute=10).timestamp()
            cached = (
                await client.call_tool(
                    "operation_result",
                    {"operation_id": started["operation_id"], "limit": 1},
                )
            ).structured_content["result"]
            self.assertEqual(cached["bookings"][0]["temporal_status"], "past")
            self.assertEqual(cached["timeline"]["ongoing_references"], [])
            self.assertEqual(cached["timeline"]["next_upcoming_reference"], "next")
        self.assertEqual(backend.calls, ["bookings"])
        self.assertEqual(service.reviews, {})
