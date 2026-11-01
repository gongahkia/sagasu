import unittest
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import booking_management as ui
from bot.fbs_account import (
    cancel_booking,
    execute_cancellation,
    CancellationOutcomeUnknown,
)
from bot.fbs_client import FBSError


BOOKING = {
    "reference_number": "BK-TEST",
    "room": "Room A",
    "date_time": "02-Nov-2026 14:00 - 16:00",
    "fbs_status": "Confirmed",
    "can_cancel": True,
}


class CancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_receipt_failure_preserves_verified_cancellation(self):
        with (
            patch("bot.fbs_account.FBSClient"),
            patch(
                "bot.fbs_account.cancel_booking",
                AsyncMock(
                    return_value={"status": "cancelled", "fbs_status": "Cancelled"}
                ),
            ) as cancel,
            patch(
                "bot.fbs_account.capture_booking_evidence",
                AsyncMock(side_effect=FBSError("unreadable")),
            ),
        ):
            result = await execute_cancellation(BOOKING, ui.REASON)
        self.assertEqual(result["status"], "cancelled")
        self.assertIn("receipt_error", result)
        cancel.assert_awaited_once()

    def client(self):
        button = Mock(click=AsyncMock())
        row = Mock()
        row.locator.return_value = button
        frame = Mock()
        page = Mock(wait_for_timeout=AsyncMock())
        page.frame.return_value = frame
        return SimpleNamespace(page=page), row, button

    async def test_changed_booking_or_missing_permission_is_rejected_before_click(self):
        for current in (
            dict(BOOKING, room="Room B"),
            dict(BOOKING, fbs_status="Pending Confirmation"),
            dict(BOOKING, can_cancel=False),
        ):
            client, row, button = self.client()
            with (
                patch("bot.fbs_account.read_bookings", AsyncMock()),
                patch(
                    "bot.fbs_account.booking_rows",
                    AsyncMock(return_value=[(current, row)]),
                ),
            ):
                with self.assertRaises(FBSError):
                    await cancel_booking(client, BOOKING, ui.REASON)
            button.click.assert_not_awaited()

    async def test_submits_once_and_requires_cancelled_status_for_success(self):
        client, row, button = self.client()
        after = dict(BOOKING, fbs_status="Cancelled")
        with (
            patch("bot.fbs_account.read_bookings", AsyncMock()),
            patch(
                "bot.fbs_account.booking_rows",
                AsyncMock(side_effect=[[(BOOKING, row)], [(after, row)]]),
            ),
        ):
            result = await cancel_booking(client, BOOKING, ui.REASON)
        self.assertEqual(result["status"], "cancelled")
        button.click.assert_awaited_once()
        client.page.remove_listener.assert_called_once()

    async def test_pending_cancellation_is_not_reported_as_cancelled(self):
        client, row, button = self.client()
        after = dict(BOOKING, fbs_status="Pending Cancellation")
        with (
            patch("bot.fbs_account.read_bookings", AsyncMock()),
            patch(
                "bot.fbs_account.booking_rows",
                AsyncMock(side_effect=[[(BOOKING, row)], [(after, row)]]),
            ),
        ):
            result = await cancel_booking(client, BOOKING, ui.REASON)
        self.assertEqual(result["status"], "pending")

    async def test_native_confirmation_is_accepted_only_for_cancelling_a_booking(self):
        for message, accepted in (
            ("Cancel this booking?", True),
            ("Continue another operation?", False),
        ):
            client, row, button = self.client()
            dialog = Mock(
                type="confirm", message=message, accept=AsyncMock(), dismiss=AsyncMock()
            )

            async def click():
                handler = client.page.on.call_args.args[1]
                await handler(dialog)

            button.click.side_effect = click
            after = dict(BOOKING, fbs_status="Cancelled")
            with (
                patch("bot.fbs_account.read_bookings", AsyncMock()),
                patch(
                    "bot.fbs_account.booking_rows",
                    AsyncMock(side_effect=[[(BOOKING, row)], [(after, row)]]),
                ),
            ):
                if accepted:
                    await cancel_booking(client, BOOKING, ui.REASON)
                    dialog.accept.assert_awaited_once()
                else:
                    with self.assertRaises(CancellationOutcomeUnknown):
                        await cancel_booking(client, BOOKING, ui.REASON)
                    dialog.dismiss.assert_awaited_once()
                    dialog.accept.assert_not_awaited()

    async def test_disappearing_row_is_uncertain_and_never_retried(self):
        client, row, button = self.client()
        calls = 0

        async def rows(frame):
            nonlocal calls
            calls += 1
            return [(BOOKING, row)] if calls == 1 else []

        with (
            patch("bot.fbs_account.read_bookings", AsyncMock()),
            patch("bot.fbs_account.booking_rows", AsyncMock(side_effect=rows)),
            patch(
                "bot.fbs_account.confirm_cancellation_dialog",
                AsyncMock(return_value=False),
            ),
        ):
            with self.assertRaises(CancellationOutcomeUnknown):
                await cancel_booking(client, BOOKING, ui.REASON)
        button.click.assert_awaited_once()


class ManagementUiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = Mock(
            chat=SimpleNamespace(type="private"),
            chat_id=7,
            reply_text=AsyncMock(),
            edit_text=AsyncMock(),
        )
        self.status = Mock(edit_text=AsyncMock())
        self.message.reply_text.return_value = self.status
        self.context = SimpleNamespace(
            user_data={
                "booking_management": {
                    "id": "list",
                    "chat_id": 7,
                    "expires_at": time.time() + 600,
                    "items": [dict(BOOKING)],
                    "total": 1,
                }
            },
            bot=Mock(),
        )

    async def click(self, data):
        query = Mock(
            data=data,
            answer=AsyncMock(),
            message=self.message,
            edit_message_text=AsyncMock(),
        )
        await ui.management_callback(
            SimpleNamespace(callback_query=query), self.context
        )

    async def test_selecting_cancel_only_creates_explicit_review_and_keep_does_nothing(
        self,
    ):
        with patch.object(ui, "execute_cancellation", AsyncMock()) as backend:
            await self.click("manage:list:choose:0")
            token = next(iter(self.context.user_data["cancellation_reviews"]))
            self.assertIn(
                "Confirming submits", self.message.reply_text.await_args.args[0]
            )
            await self.click(f"manage:{token}:keep")
        backend.assert_not_awaited()

    async def test_confirmation_is_single_use_and_unknown_outcome_invalidates_list(
        self,
    ):
        await self.click("manage:list:choose:0")
        token = next(iter(self.context.user_data["cancellation_reviews"]))
        with patch.object(
            ui,
            "execute_cancellation",
            AsyncMock(side_effect=CancellationOutcomeUnknown("Check FBS")),
        ) as backend:
            await self.click(f"manage:{token}:confirm")
            await self.click(f"manage:{token}:confirm")
        backend.assert_awaited_once()
        self.assertNotIn("booking_management", self.context.user_data)

    async def test_expired_review_does_not_cancel(self):
        self.context.user_data["cancellation_reviews"] = {
            "old": {"chat_id": 7, "expires_at": time.time() - 1, "booking": BOOKING}
        }
        with patch.object(ui, "execute_cancellation", AsyncMock()) as backend:
            await self.click("manage:old:confirm")
        backend.assert_not_awaited()

    async def test_bookings_list_is_private_and_loading_it_never_submits_cancellation(
        self,
    ):
        self.message.chat.type = "group"
        with patch.object(ui, "fetch_bookings", AsyncMock()) as backend:
            await ui.show_bookings(self.message, self.context)
        backend.assert_not_awaited()
        self.message.chat.type = "private"
        with patch.object(
            ui,
            "fetch_bookings",
            AsyncMock(return_value={"bookings": [BOOKING], "total": 1}),
        ):
            with patch.object(ui, "execute_cancellation", AsyncMock()) as cancel:
                await ui.show_bookings(self.message, self.context)
        cancel.assert_not_awaited()

    async def test_other_chat_cannot_use_cancellation_review(self):
        self.context.user_data["cancellation_reviews"] = {
            "other": {"chat_id": 88, "expires_at": time.time() + 60, "booking": BOOKING}
        }
        with patch.object(ui, "execute_cancellation", AsyncMock()) as backend:
            await self.click("manage:other:confirm")
        backend.assert_not_awaited()
