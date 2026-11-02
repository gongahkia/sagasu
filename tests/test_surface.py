import asyncio
import pickle
import unittest
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

from bot import bot, booking_management as management, surface, task_management
from bot.fbs_client import (
    FBS_TIMEZONE,
    SearchConfig,
    SearchResult,
    RoomAvailability,
    TimeSlot,
)


class FakeBot:
    def __init__(self):
        self.messages = {}
        self.next_id = 100
        self.sent_texts = 0
        self.sent_receipts = 0
        self.media_edits = 0

    async def send_message(self, chat_id, **payload):
        self.next_id += 1
        self.sent_texts += 1
        self.messages[self.next_id] = dict(payload, kind="text", author="bot")
        return NS(message_id=self.next_id)

    async def edit_message_text(self, chat_id, message_id, **payload):
        if message_id not in self.messages:
            raise BadRequest("Message to edit not found")
        self.messages[message_id] = dict(payload, kind="text", author="bot")

    async def delete_message(self, chat_id, message_id):
        self.messages.pop(message_id, None)

    async def send_photo(self, chat_id, photo, caption, **kwargs):
        self.next_id += 1
        self.sent_receipts += 1
        self.messages[self.next_id] = dict(kind="photo", caption=caption, author="bot")
        return NS(message_id=self.next_id)

    async def send_document(self, **kwargs):
        return await self.send_photo(
            kwargs["chat_id"], kwargs["document"], kwargs["caption"]
        )

    async def edit_message_media(self, chat_id, message_id, media):
        if message_id not in self.messages:
            raise BadRequest("Message to edit not found")
        self.media_edits += 1
        self.messages[message_id] = dict(
            kind="photo", caption=media.caption, author="bot"
        )


class SurfaceTest(unittest.IsolatedAsyncioTestCase):
    async def open_task(self):
        task = {
            "reference_number": "BK-TASK",
            "room": "Room A",
            "date_time": "03-Nov-2026 14:00 - 16:00",
            "request_type": "Booking Approval",
            "requested_by": "A student",
            "status": "Pending Approval",
            "can_approve": True,
            "can_view": True,
        }
        await self.command(bot.start)
        with patch.object(
            bot,
            "check_task_list",
            AsyncMock(return_value={"tasks": [task], "total": 1, "complete": True}),
        ):
            await self.click("FBS Task List")
        return task

    async def test_task_approval_review_back_and_confirmation_use_one_panel(self):
        task = await self.open_task()
        result = {
            "status": "approved",
            "task_status": "Approval acknowledged by FBS",
            "task": task,
            "reference_number": task["reference_number"],
            "receipt_png": b"png",
            "receipt_kind": "approval_acknowledgement",
        }
        with patch.object(
            task_management, "execute_task_action", AsyncMock(return_value=result)
        ) as execute:
            await self.click("Review approval")
            execute.assert_not_awaited()
            await self.click("Back to tasks")
            execute.assert_not_awaited()
            await self.click("Review approval")
            old_confirmation = self.callback_data("Confirm task approval")
            await self.click("Confirm task approval")
            execute.assert_awaited_once()
            self.assertTrue(execute.await_args.kwargs["approve"])
            await self.click(data=old_confirmation)
            execute.assert_awaited_once()
        self.assertEqual(self.api.sent_texts, 1)
        self.assertEqual(self.api.sent_receipts, 1)
        self.assertIn("Task approval verified", self.current()["text"])
        self.assertIn("may still require", self.current()["text"])
        self.assertNotIn(
            "task_approval_reviews",
            {key: value for key, value in self.context.user_data.items() if value},
        )

    async def test_task_details_sends_screenshot_without_approval(self):
        task = await self.open_task()
        result = {
            "status": "details",
            "task": task,
            "reference_number": task["reference_number"],
            "receipt_png": b"png",
            "receipt_kind": "booking_details",
        }
        with patch.object(
            task_management, "execute_task_action", AsyncMock(return_value=result)
        ) as execute:
            await self.click("Details")
            self.assertNotIn("approve", execute.await_args.kwargs)
        self.assertEqual(self.api.sent_texts, 1)
        self.assertEqual(self.api.sent_receipts, 1)
        self.assertIn("Task List", self.current()["text"])

    async def test_expired_task_review_never_calls_backend(self):
        await self.open_task()
        await self.click("Review approval")
        for review in self.context.user_data["task_approval_reviews"].values():
            review["expires_at"] = 0
        with patch.object(
            task_management, "execute_task_action", AsyncMock()
        ) as execute:
            await self.click("Confirm task approval")
            execute.assert_not_awaited()
        self.assertIn("expired", self.current()["text"])

    def setUp(self):
        surface._locks.clear()
        surface._active.clear()
        self.api = FakeBot()
        self.context = NS(bot=self.api, user_data={}, args=[])
        self.chat = NS(id=7, type="private")
        self.user_id = 0
        clock = patch("bot.fbs_client.datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.return_value = datetime(2026, 11, 1, 13, 24, tzinfo=FBS_TIMEZONE)
        self.addCleanup(clock.stop)

    def state(self):
        return surface.conversation_state(self.context, 7)

    def current(self):
        return self.api.messages[self.state()["message_id"]]

    async def command(self, handler, text="/start"):
        self.user_id += 1
        message = NS(chat_id=7, chat=self.chat, message_id=self.user_id, text=text)
        self.api.messages[self.user_id] = dict(kind="text", author="user", text=text)
        update = NS(
            message=message,
            callback_query=None,
            effective_message=message,
            effective_user=NS(id=7),
        )
        await surface.surface_handler(
            handler, personal_store=getattr(self, "personal_store", None)
        )(update, self.context)

    def callback_data(self, label):
        for row in self.current()["reply_markup"].inline_keyboard:
            for button in row:
                if label in button.text:
                    return button.callback_data
        self.fail(f"No button {label!r} on {self.current()['text']!r}")

    async def click(self, label=None, data=None):
        data = data or self.callback_data(label)
        message = NS(
            chat_id=7,
            chat=self.chat,
            message_id=self.state()["message_id"],
            text=self.current()["text"],
        )
        query = NS(data=data, message=message, from_user=NS(id=7), answer=AsyncMock())
        update = NS(
            message=None,
            callback_query=query,
            effective_message=message,
            effective_user=NS(id=7),
        )
        await surface.surface_handler(
            bot.surface_callback, personal_store=getattr(self, "personal_store", None)
        )(update, self.context)
        return query

    def search_result(self, count=12):
        config = SearchConfig("01-Nov-2026", "13:30", "15:30")
        return SearchResult(
            config,
            [
                RoomAvailability(f"Room {index}", [TimeSlot("13:30", "15:30", "free")])
                for index in range(count)
            ],
            "test",
        )

    async def search(self):
        with patch.object(
            bot, "search_fbs", AsyncMock(return_value=self.search_result())
        ):
            await self.click("Find available rooms")

    async def review(self):
        await self.search()
        await self.click("Room 0")
        await self.click("Book this room")
        await self.command(bot.handle_text_input, "Project meeting")
        await self.command(bot.handle_text_input, "friend@smu.edu.sg")
        await self.click("Use once")

    async def test_home_settings_results_and_pagination_share_one_message(self):
        await self.command(bot.start)
        await self.click("Search settings")
        await self.click("Start:")
        await self.click("Next half-hour")
        await self.click("Home")
        await self.search()
        await self.click("Next →")
        self.assertIn("Page 2 of 2", self.current()["text"])
        await self.click("Room 9")
        await self.click("← Back")
        self.assertIn("Page 2 of 2", self.current()["text"])
        await self.click("Home")
        self.assertEqual(self.api.sent_texts, 1)
        self.assertEqual(len(self.api.messages), 1)

    async def test_shared_personal_data_through_real_surface_binding(self):
        import tempfile
        from pathlib import Path
        from bot.personal import PersonalStore

        with tempfile.TemporaryDirectory() as directory:
            self.personal_store = PersonalStore(Path(directory) / "personal.sqlite3")
            profile = self.personal_store.for_user(7)
            self.context.user_data["saved_cobookers"] = {
                "legacy": {"name": "Old friend", "email": "old@smu.edu.sg"}
            }
            await self.command(bot.start)
            self.assertIn("legacy", profile.contacts())
            person = profile.save_contact("MCP friend", "new@smu.edu.sg")
            await self.click("Saved co-bookers")
            self.assertIn("MCP friend", self.current()["text"])
            await self.click("Remove MCP friend")
            self.assertNotIn(person["id"], profile.contacts())
            await self.click("Home")
            profile.update_preferences({"duration_hrs": 3})
            await self.click("Search settings")
            self.assertIn("Duration: 3h", str(self.current()["reply_markup"]))
            await self.click("Duration:")
            await self.click("1.5h")
            self.assertEqual(profile.preferences()["duration_hrs"], 1.5)
            await self.click("Reset to defaults")
            self.assertEqual(profile.preferences(), {})
            self.assertIn("legacy", profile.contacts())
            self.assertEqual(self.api.sent_texts, 1)

    async def test_booking_questions_validation_and_receipt_use_two_messages_total(
        self,
    ):
        await self.command(bot.start)
        await self.review()
        with patch.object(
            bot, "execute_booking", AsyncMock(return_value={"status": "ready"})
        ):
            await self.click("Validate only")
        self.assertIn("Validation passed", self.current()["text"])
        self.callback_data("Confirm & book")
        result = dict(
            status="verified",
            reference_number="BK-TEST",
            fbs_status="Pending Confirmation",
            receipt_png=b"png",
            room="Room 0",
            date="01-Nov-2026",
            start_time="13:30",
            end_time="15:30",
        )
        with patch.object(
            bot, "execute_booking", AsyncMock(return_value=result)
        ) as backend:
            await self.click("Confirm & book")
        backend.assert_awaited_once()
        self.assertEqual(self.api.sent_texts, 1)
        self.assertEqual(self.api.sent_receipts, 1)
        self.assertEqual(len(self.api.messages), 2)
        self.assertNotIn("receipt_png", repr(self.context.user_data))
        pickle.dumps(self.context.user_data)

    async def test_cancellation_review_back_and_confirmation_replace_existing_receipt(
        self,
    ):
        await self.command(bot.start)
        await surface.deliver_receipt(self.context, 7, b"old", "Existing receipt")
        booking = dict(
            reference_number="BK-TEST",
            room="Room A",
            date_time="02-Nov-2026 14:00–16:00",
            fbs_status="Confirmed",
            can_cancel=True,
        )
        with patch.object(
            management,
            "fetch_bookings",
            AsyncMock(return_value={"bookings": [booking], "total": 1}),
        ):
            await self.click("My bookings")
        with patch.object(
            management,
            "execute_cancellation",
            AsyncMock(
                return_value={
                    "status": "cancelled",
                    "fbs_status": "Cancelled",
                    "receipt_png": b"new",
                }
            ),
        ) as backend:
            await self.click("Cancel Room")
            await self.click("← Back")
            backend.assert_not_awaited()
            self.assertIn("FBS My Bookings", self.current()["text"])
            await self.click("Cancel Room")
            await self.click("Confirm cancellation")
        backend.assert_awaited_once()
        self.assertIn("Cancellation verified", self.current()["text"])
        self.assertEqual(self.api.sent_receipts, 1)
        self.assertEqual(self.api.media_edits, 1)
        self.assertEqual(len(self.api.messages), 2)

    async def test_old_screen_buttons_do_not_change_current_state(self):
        await self.command(bot.start)
        old = self.callback_data("Search settings")
        await self.click("Help")
        text = self.current()["text"]
        query = await self.click(data=old)
        query.answer.assert_awaited_once()
        self.assertEqual(self.current()["text"], text)
        self.assertEqual(len(self.api.messages), 1)

    async def test_mfa_uses_control_message_without_persisting_matching_number(self):
        await self.command(bot.start)

        async def operation(update, context):
            notify = bot.owner_auth_notifier(context.bot, render=context.ui_auth_notify)
            await notify("Open Authenticator. Enter number 88.")
            self.assertIn("Enter number 88", self.current()["text"])
            self.assertNotIn("Enter number 88", repr(self.context.user_data))

        with patch.dict("os.environ", {"TELEGRAM_OWNER_ID": "7"}):
            await self.command(operation, "/test")
        self.assertEqual(self.api.sent_texts, 1)

    async def test_critical_submission_is_not_interrupted_by_home_command(self):
        await self.command(bot.start)
        await self.review()
        entered, release = asyncio.Event(), asyncio.Event()

        async def execute(*args, **kwargs):
            entered.set()
            await release.wait()
            return dict(
                status="verified", reference_number="BK-TEST", fbs_status="Confirmed"
            )

        with patch.object(bot, "execute_booking", AsyncMock(side_effect=execute)):
            task = asyncio.create_task(self.click("Confirm & book"))
            await entered.wait()
            await self.command(bot.start)
            self.assertFalse(task.done())
            release.set()
            await task
        self.assertIn("Booking recorded", self.current()["text"])
        self.assertEqual(len(self.api.messages), 1)

    async def test_home_cancels_read_and_late_progress_cannot_overwrite_home(self):
        await self.command(bot.start)
        entered = asyncio.Event()

        async def search(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(bot, "search_fbs", AsyncMock(side_effect=search)):
            task = asyncio.create_task(self.click("Find available rooms"))
            await entered.wait()
            await self.click("Home")
            await task
        self.assertIn("Sagasu", self.current()["text"])
        self.assertEqual(len(self.api.messages), 1)

    async def test_deleted_control_message_is_replaced_without_duplicate_live_panels(
        self,
    ):
        await self.command(bot.start)
        await self.api.delete_message(7, self.state()["message_id"])
        await self.command(bot.start)
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(self.api.sent_texts, 2)

    async def test_persisted_surface_reuses_control_and_receipt_after_context_reload(
        self,
    ):
        await self.command(bot.start)
        await surface.deliver_receipt(self.context, 7, b"old", "Old receipt")
        self.context.user_data = pickle.loads(pickle.dumps(self.context.user_data))
        await self.command(bot.start)
        await surface.deliver_receipt(self.context, 7, b"new", "New receipt")
        self.assertEqual(self.api.sent_texts, 1)
        self.assertEqual(self.api.sent_receipts, 1)
        self.assertEqual(len(self.api.messages), 2)

    async def test_failed_edit_releases_active_operation_and_next_command_recovers(
        self,
    ):
        await self.command(bot.start)
        with patch.object(
            self.api,
            "edit_message_text",
            AsyncMock(side_effect=BadRequest("test rejection")),
        ):
            with self.assertRaises(BadRequest):
                await self.command(bot.help_command, "/help")
        self.assertFalse(surface._active)
        await self.command(bot.start)
        self.assertEqual(len(self.api.messages), 1)

    async def test_review_back_reopens_purpose_without_submitting(self):
        await self.command(bot.start)
        await self.review()
        with patch.object(bot, "execute_booking", AsyncMock()) as backend:
            await self.click("← Back")
        backend.assert_not_awaited()
        self.assertIn("What is this booking for", self.current()["text"])
        self.assertIn("Previous purpose: Project meeting", self.current()["text"])
        self.assertEqual(len(self.api.messages), 1)

    async def test_invalid_answers_keep_question_and_usable_buttons(self):
        await self.command(bot.start)
        await self.search()
        await self.click("Room 0")
        await self.click("Book this room")
        await self.command(bot.handle_text_input, "Project meeting")
        await self.command(bot.handle_text_input, "friend")
        self.assertIn("full SMU email", self.current()["text"])
        self.callback_data("Enter another")
        await self.command(bot.handle_text_input, "friend@smu.edu.sg")
        await self.command(bot.handle_text_input, "something unexpected")
        self.assertIn("Choose an option below", self.current()["text"])
        await self.click("Use once")
        self.assertIn("Review booking", self.current()["text"])
        self.assertEqual(len(self.api.messages), 1)

    async def test_tasks_are_paged_inside_control_message(self):
        await self.command(bot.start)
        tasks = [
            dict(
                reference_number=f"BK-{i}",
                room=f"Room {i}",
                date_time="Tomorrow",
                request_type="Approval",
                requested_by="Friend",
                status="Pending Approval",
            )
            for i in range(9)
        ]
        with patch.object(
            bot,
            "check_task_list",
            AsyncMock(return_value={"tasks": tasks, "total": 9, "complete": True}),
        ):
            await self.click("FBS Task List")
        self.assertNotIn("Room 4", self.current()["text"])
        await self.click("Next →")
        self.assertIn("Room 4", self.current()["text"])
        self.assertEqual(len(self.api.messages), 1)

    async def test_slow_initial_send_is_tracked_before_navigation_replaces_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api.send_message

        async def slow_send(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        with patch.object(self.api, "send_message", slow_send):
            first = asyncio.create_task(self.command(bot.start))
            await entered.wait()
            second = asyncio.create_task(self.command(bot.help_command, "/help"))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertIsNone(results[1])
        self.assertEqual(self.api.sent_texts, 1)
        self.assertEqual(len(self.api.messages), 1)

    async def test_oversized_photo_keeps_same_receipt_slot_when_document_succeeds(self):
        await self.command(bot.start)
        await surface.deliver_receipt(self.context, 7, b"old", "Old receipt")
        original = self.api.edit_message_media

        async def edit(*args, **kwargs):
            if isinstance(kwargs["media"], surface.InputMediaPhoto):
                raise BadRequest("PHOTO_INVALID_DIMENSIONS")
            return await original(*args, **kwargs)

        with patch.object(self.api, "edit_message_media", edit):
            await surface.deliver_receipt(self.context, 7, b"new", "New receipt")
        self.assertEqual(self.api.sent_receipts, 1)
        self.assertEqual(len(self.api.messages), 2)
        self.assertEqual(
            self.api.messages[self.state()["receipt_message_id"]]["caption"],
            "New receipt",
        )
