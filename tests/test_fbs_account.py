import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sagasu.fbs_account import capture_booking_evidence, parse_tasks
from sagasu.fbs_client import (
    BookingRequest,
    FBSClient,
    LayoutChanged,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
)


HEADERS = [
    "REF. NUMBER",
    "DATE & TIME OF USE",
    "BUILDING",
    "FACILITY",
    "TYPE OF REQUEST",
    "REQUESTED BY",
    "STATUS",
    "",
]
ROW = [
    "BK-TEST",
    "02-Nov-2026 14:00 - 15:00",
    "SCIS1",
    "Room A",
    "Booking Approval",
    "A student",
    "Pending Approval",
    "VIEW",
]


def task_snapshot(rows=None, total=1):
    return {
        "headers": list(HEADERS),
        "rows": [list(ROW)] if rows is None else rows,
        "summary": f"{'No items ' if total == 0 else ''}{total} ITEM(S) TOTAL",
    }


class TaskParsingTest(unittest.TestCase):
    def test_pending_approval_maps_columns_without_returning_action_controls(self):
        result = parse_tasks(task_snapshot())
        self.assertEqual(
            result["tasks"][0],
            {
                "reference_number": "BK-TEST",
                "date_time": ROW[1],
                "room": "Room A",
                "request_type": "Booking Approval",
                "requested_by": "A student",
                "status": "Pending Approval",
            },
        )
        self.assertTrue(result["complete"])

    def test_verified_empty_list(self):
        self.assertEqual(
            parse_tasks(task_snapshot([["No items"]], 0)),
            {"tasks": [], "total": 0, "complete": True},
        )

    def test_unloaded_table_or_missing_rows_cannot_claim_no_tasks(self):
        invalid = [None, {}, task_snapshot([], 3)]
        empty = task_snapshot([], 0)
        empty["summary"] = "0 ITEM(S) TOTAL"
        invalid.append(empty)
        for snapshot in invalid:
            with self.subTest(snapshot=snapshot), self.assertRaises(LayoutChanged):
                parse_tasks(snapshot)

    def test_changed_columns_and_missing_status_fail_closed(self):
        changed = task_snapshot()
        changed["headers"][6] = "Other"
        missing = task_snapshot()
        missing["rows"][0][6] = ""
        truncated = task_snapshot()
        truncated["rows"][0].pop()
        for snapshot in (changed, missing, truncated):
            with self.subTest(snapshot=snapshot), self.assertRaises(LayoutChanged):
                parse_tasks(snapshot)

    def test_partial_page_reports_total_and_incomplete_state(self):
        result = parse_tasks(task_snapshot(total=40))
        self.assertFalse(result["complete"])
        self.assertEqual(result["total"], 40)


class EvidenceTest(unittest.IsolatedAsyncioTestCase):
    def setup_evidence(self, detail_reference="BK-TEST"):
        client = FBSClient(progress=AsyncMock())
        client._wait_for_idle = AsyncMock()
        rows = []
        views = []
        for ref in ("BK-OTHER", "BK-TEST"):
            view = Mock(count=AsyncMock(return_value=1), click=AsyncMock())
            cell = Mock(inner_text=AsyncMock(return_value=ref))
            cells = Mock(count=AsyncMock(return_value=10))
            cells.nth.return_value = cell
            row = Mock()
            row.locator.side_effect = lambda selector, v=view, c=cells: (
                c if selector == "td" else v
            )
            rows.append(row)
            views.append(view)
        listing = Mock()
        listing.locator.return_value.all = AsyncMock(return_value=rows)
        number = Mock(is_visible=AsyncMock(return_value=True))
        identity = Mock(
            count=AsyncMock(return_value=int(detail_reference == "BK-TEST")),
            is_visible=AsyncMock(return_value=True),
        )
        number.get_by_text.return_value = identity
        body = Mock(screenshot=AsyncMock(return_value=b"png-bytes"))
        detail = Mock(wait_for_load_state=AsyncMock())
        detail.get_by_text.return_value.all = AsyncMock(return_value=[])
        detail.locator.side_effect = lambda selector: (
            number if selector == "#bookingFormControl1_ObjectNumber" else body
        )
        client.page = Mock()
        client.page.frames = [detail]
        client.page.frame.return_value = detail
        return client, listing, views, body

    async def test_opens_only_exact_reference_view_and_captures_details(self):
        client, listing, views, body = self.setup_evidence()
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(return_value=listing)
        ):
            self.assertEqual(
                await capture_booking_evidence(client, "BK-TEST"), b"png-bytes"
            )
        views[0].click.assert_not_awaited()
        views[1].click.assert_awaited_once()
        body.screenshot.assert_awaited_once()

    async def test_parent_loading_notice_is_hidden_before_receipt_capture(self):
        client, listing, _, body = self.setup_evidence()
        notice = Mock(wait_for=AsyncMock())
        parent = Mock()
        parent.get_by_text.return_value.all = AsyncMock(return_value=[notice])
        client.page.frames.insert(0, parent)
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(return_value=listing)
        ):
            await capture_booking_evidence(client, "BK-TEST")
        notice.wait_for.assert_awaited_once_with(state="hidden", timeout=15000)
        body.screenshot.assert_awaited_once()

    async def test_wrong_details_reference_never_sends_a_receipt(self):
        client, listing, _, body = self.setup_evidence("BK-OTHER")
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(return_value=listing)
        ):
            with self.assertRaises(LayoutChanged):
                await capture_booking_evidence(client, "BK-TEST")
        body.screenshot.assert_not_awaited()

    async def test_verified_booking_survives_capture_failure(self):
        config = SearchConfig("02-Nov-2026", "14:00", "15:00")
        request = BookingRequest(config, "Room A", "Meeting", "friend@smu.edu.sg")
        client = FBSClient()
        client.page = Mock(wait_for_timeout=AsyncMock())
        client.search = AsyncMock(
            return_value=SearchResult(
                config,
                [RoomAvailability("Room A", [TimeSlot("14:00", "15:00", "free")])],
                "test",
            )
        )
        control = Mock(
            fill=AsyncMock(),
            select_option=AsyncMock(),
            check=AsyncMock(),
            uncheck=AsyncMock(),
            click=AsyncMock(),
        )
        control.first = control
        form = Mock()
        form.locator.return_value = control
        client._prepare_booking_frame = AsyncMock(return_value=form)
        client._wait_for_idle = AsyncMock()
        client._add_co_booker = AsyncMock()
        client._verify_booking_details = AsyncMock()
        client._verify_booking = AsyncMock(
            return_value={"status": "verified", "reference_number": "BK-TEST"}
        )
        with (
            patch("sagasu.fbs_client.require_future_booking"),
            patch(
                "sagasu.fbs_account.capture_booking_evidence",
                AsyncMock(side_effect=LayoutChanged("changed")),
            ),
        ):
            result = await client.book(request, submit=True)
        self.assertEqual(result["status"], "verified")
        self.assertIn("receipt_error", result)
        self.assertNotIn("receipt_png", result)
        control.click.assert_awaited_once()
