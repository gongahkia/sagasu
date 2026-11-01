import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import bot
from bot.fbs_account import capture_booking_evidence, parse_tasks
from bot.fbs_client import (
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
        with patch("bot.fbs_account.open_account_tab", AsyncMock(return_value=listing)):
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
        with patch("bot.fbs_account.open_account_tab", AsyncMock(return_value=listing)):
            await capture_booking_evidence(client, "BK-TEST")
        notice.wait_for.assert_awaited_once_with(state="hidden", timeout=15000)
        body.screenshot.assert_awaited_once()

    async def test_wrong_details_reference_never_sends_a_receipt(self):
        client, listing, _, body = self.setup_evidence("BK-OTHER")
        with patch("bot.fbs_account.open_account_tab", AsyncMock(return_value=listing)):
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
            patch("bot.fbs_client.require_future_booking"),
            patch(
                "bot.fbs_account.capture_booking_evidence",
                AsyncMock(side_effect=LayoutChanged("changed")),
            ),
        ):
            result = await client.book(request, submit=True)
        self.assertEqual(result["status"], "verified")
        self.assertIn("receipt_error", result)
        self.assertNotIn("receipt_png", result)
        control.click.assert_awaited_once()


class TelegramAccountTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = Mock(
            chat=SimpleNamespace(type="private"), reply_text=AsyncMock()
        )
        self.status = Mock(edit_text=AsyncMock())
        self.message.reply_text.return_value = self.status
        self.context = SimpleNamespace(
            bot=Mock(send_photo=AsyncMock(), send_document=AsyncMock())
        )

    async def test_receipt_is_sent_to_user_privately_and_falls_back_to_original_png(
        self,
    ):
        self.context.bot.send_photo.side_effect = RuntimeError("too tall")
        query = SimpleNamespace(from_user=SimpleNamespace(id=7), message=self.message)
        await bot.send_booking_receipt(
            query, self.context, {"reference_number": "BK-TEST", "receipt_png": b"png"}
        )
        self.assertEqual(self.context.bot.send_photo.await_args.kwargs["chat_id"], 7)
        self.assertEqual(self.context.bot.send_document.await_args.kwargs["chat_id"], 7)
        self.assertEqual(
            self.context.bot.send_document.await_args.kwargs["document"], b"png"
        )

    async def test_delivery_failure_preserves_confirmed_message(self):
        self.context.bot.send_photo.side_effect = RuntimeError()
        self.context.bot.send_document.side_effect = RuntimeError()
        query = SimpleNamespace(from_user=SimpleNamespace(id=7), message=self.message)
        await bot.send_booking_receipt(
            query, self.context, {"reference_number": "BK-TEST", "receipt_png": b"png"}
        )
        self.assertIn("booking is recorded", self.message.reply_text.await_args.args[0])

    async def test_tasks_rejects_group_chat_without_accessing_fbs(self):
        self.message.chat.type = "group"
        with patch.object(bot, "check_task_list", AsyncMock()) as backend:
            await bot.show_task_list(self.message, self.context)
        backend.assert_not_awaited()

    async def test_tasks_empty_success_and_backend_failure_are_distinct(self):
        with patch.object(
            bot,
            "check_task_list",
            AsyncMock(return_value=parse_tasks(task_snapshot([], 0))),
        ):
            await bot.show_task_list(self.message, self.context)
        self.assertIn("Task List is empty", self.status.edit_text.await_args.args[0])
        with patch.object(
            bot, "check_task_list", AsyncMock(side_effect=LayoutChanged("unreadable"))
        ):
            await bot.show_task_list(self.message, self.context)
        self.assertIn("Could not check", self.status.edit_text.await_args.args[0])

    async def test_pending_tasks_show_request_and_status_with_partial_warning(self):
        with patch.object(
            bot,
            "check_task_list",
            AsyncMock(return_value=parse_tasks(task_snapshot(total=40))),
        ):
            await bot.show_task_list(self.message, self.context)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("Pending Approval", text)
        self.assertIn("Booking Approval", text)
        self.assertIn("Showing 1 of 40", text)
