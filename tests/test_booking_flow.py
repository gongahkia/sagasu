import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.ext import PicklePersistence

from bot import bot
from bot.booking_flow import (
    CONTACTS_KEY,
    FLOW_KEY,
    accept_text,
    begin_booking,
    cobookers_command,
    open_contacts,
    save_contact,
    wizard_callback,
)


class BookingFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = Mock(chat_id=100, text="", reply_text=AsyncMock())
        self.context = SimpleNamespace(
            user_data={
                "selected_room": "SCIS1 GSR 3-1",
                "last_search": {
                    "config": {
                        "date": "02-Nov-2026",
                        "start_time": "13:00",
                        "end_time": "14:00",
                    }
                },
            },
            args=[],
            bot=Mock(),
        )
        self.execute = patch.object(bot, "execute_booking", AsyncMock(return_value={}))
        self.backend = self.execute.start()
        self.addCleanup(self.execute.stop)

    def update(self, message=None):
        message = message or self.message
        return SimpleNamespace(message=message, effective_message=message)

    def flow(self):
        return self.context.user_data[FLOW_KEY]

    async def text(self, text):
        self.message.text = text
        await bot.handle_text_input(self.update(), self.context)

    async def click(self, action, *, token=None, message=None):
        query = Mock(
            data=f"wizard:{token or self.flow()['id']}:{action}",
            message=message or self.message,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        await wizard_callback(SimpleNamespace(callback_query=query), self.context)
        return query

    def last_text(self):
        return self.message.reply_text.await_args.args[0]

    async def purpose_and_email(self):
        await begin_booking(self.message, self.context)
        await self.text("Project meeting")
        await self.text("friend@smu.edu.sg")

    async def test_guided_booking_saves_named_contact_then_reviews_without_submission(
        self,
    ):
        await bot.book_command(self.update(), self.context)
        self.assertEqual(self.flow()["step"], "purpose")
        await self.text("Project meeting")
        self.assertEqual(self.flow()["step"], "co_booker")
        await self.text("Friend@scis.smu.edu.sg")
        self.assertEqual(self.flow()["step"], "save_choice")
        await self.click("save")
        self.assertEqual(self.flow()["step"], "save_name")
        await self.text("Lexuan")
        self.assertNotIn(FLOW_KEY, self.context.user_data)
        self.assertIn(
            {"name": "Lexuan", "email": "friend@scis.smu.edu.sg"},
            self.context.user_data[CONTACTS_KEY].values(),
        )
        draft = next(iter(self.context.user_data["booking_drafts"].values()))
        self.assertEqual(draft["request"]["purpose"], "Project meeting")
        self.assertEqual(draft["request"]["room"], "SCIS1 GSR 3-1")
        self.assertIn("Review booking", self.last_text())
        self.backend.assert_not_awaited()

    async def test_use_once_does_not_save_contact(self):
        await self.purpose_and_email()
        await self.click("skip")
        self.assertFalse(self.context.user_data[CONTACTS_KEY])
        self.assertEqual(len(self.context.user_data["booking_drafts"]), 1)
        self.backend.assert_not_awaited()

    async def test_saved_contact_reused_by_stable_id(self):
        save_contact(self.context, "Friend", "friend@smu.edu.sg")
        key = next(iter(self.context.user_data[CONTACTS_KEY]))
        await begin_booking(self.message, self.context)
        await self.text("Meeting")
        buttons = self.message.reply_text.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        self.assertTrue(
            any(
                f":use:{key}" in button.callback_data
                for row in buttons
                for button in row
            )
        )
        await self.click(f"use:{key}")
        self.assertEqual(
            next(iter(self.context.user_data["booking_drafts"].values()))["request"][
                "co_booker"
            ],
            "friend@smu.edu.sg",
        )
        self.backend.assert_not_awaited()

    async def test_username_is_rejected_and_question_is_preserved(self):
        await begin_booking(self.message, self.context)
        await self.text("Meeting")
        await self.text("lexuantan")
        self.assertEqual(self.flow()["step"], "co_booker")
        self.assertIn("full SMU email", self.last_text())
        self.assertNotIn("booking_drafts", self.context.user_data)

    async def test_already_saved_email_skips_save_question(self):
        save_contact(self.context, "Friend", "friend@smu.edu.sg")
        await self.purpose_and_email()
        self.assertNotIn(FLOW_KEY, self.context.user_data)
        self.assertEqual(len(self.context.user_data[CONTACTS_KEY]), 1)

    async def test_manager_adds_and_removes_saved_contact(self):
        await cobookers_command(self.update(), self.context)
        await self.click("add")
        await self.text("Friend")
        self.assertEqual(self.flow()["step"], "contact_email")
        await self.text("FRIEND@SMU.EDU.SG")
        self.assertEqual(self.flow()["step"], "contacts")
        key = next(iter(self.context.user_data[CONTACTS_KEY]))
        await self.click(f"remove:{key}")
        self.assertFalse(self.context.user_data[CONTACTS_KEY])
        self.backend.assert_not_awaited()

    async def test_duplicate_email_updates_name_instead_of_duplicating(self):
        save_contact(self.context, "First", "friend@smu.edu.sg")
        save_contact(self.context, "Second", "FRIEND@SMU.EDU.SG")
        self.assertEqual(
            list(self.context.user_data[CONTACTS_KEY].values()),
            [{"name": "Second", "email": "friend@smu.edu.sg"}],
        )

    async def test_contact_manager_paginates_and_removes_by_id(self):
        for index in range(10):
            save_contact(self.context, f"Friend {index}", f"friend{index}@smu.edu.sg")
        await open_contacts(self.message, self.context)
        await self.click("page:1")
        self.assertIn("Friend 9", self.last_text())
        key = next(
            key
            for key, person in self.context.user_data[CONTACTS_KEY].items()
            if person["name"] == "Friend 9"
        )
        await self.click(f"remove:{key}")
        self.assertEqual(len(self.context.user_data[CONTACTS_KEY]), 9)
        self.assertNotIn(key, self.context.user_data[CONTACTS_KEY])

    async def test_old_wizard_buttons_cannot_change_new_booking(self):
        await self.purpose_and_email()
        old = self.flow()["id"]
        await begin_booking(self.message, self.context)
        query = await self.click("skip", token=old)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertEqual(self.flow()["step"], "purpose")
        self.assertNotIn("booking_drafts", self.context.user_data)

    async def test_old_question_button_cannot_skip_new_question(self):
        await self.purpose_and_email()
        await self.click("save")
        query = await self.click("skip")
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertEqual(self.flow()["step"], "save_name")
        self.assertNotIn("booking_drafts", self.context.user_data)

    async def test_expired_flow_does_not_create_draft(self):
        await self.purpose_and_email()
        token = self.flow()["id"]
        self.flow()["expires_at"] = 0
        query = await self.click("skip", token=token)
        self.assertIn("expired", query.answer.await_args.args[0])
        self.assertNotIn("booking_drafts", self.context.user_data)

    async def test_other_chat_does_not_consume_answers_or_callbacks(self):
        await self.purpose_and_email()
        other = Mock(chat_id=999, reply_text=AsyncMock())
        self.assertFalse(await accept_text(other, self.context, "Another person"))
        query = await self.click("skip", message=other)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertEqual(self.flow()["step"], "save_choice")

    async def test_room_and_time_are_snapshotted_at_start_of_questions(self):
        await self.purpose_and_email()
        self.context.user_data["selected_room"] = "Another room"
        self.context.user_data["last_search"]["config"]["start_time"] = "15:00"
        await self.click("skip")
        request = next(iter(self.context.user_data["booking_drafts"].values()))[
            "request"
        ]
        self.assertEqual(request["room"], "SCIS1 GSR 3-1")
        self.assertEqual(request["search"]["start_time"], "13:00")

    async def test_cancel_discards_questions_and_drafts_but_keeps_contacts(self):
        await self.purpose_and_email()
        save_contact(self.context, "Keep", "keep@smu.edu.sg")
        self.context.user_data["booking_drafts"] = {"old": {}}
        await bot.cancel_command(self.update(), self.context)
        self.assertNotIn(FLOW_KEY, self.context.user_data)
        self.assertNotIn("booking_drafts", self.context.user_data)
        self.assertEqual(len(self.context.user_data[CONTACTS_KEY]), 1)
        self.backend.assert_not_awaited()

    async def test_back_allows_changing_purpose(self):
        await begin_booking(self.message, self.context)
        await self.text("Old")
        await self.click("back")
        await self.text("New")
        self.assertEqual(self.flow()["purpose"], "New")
        self.assertEqual(self.flow()["step"], "co_booker")

    async def test_purpose_and_name_are_length_limited_and_html_escaped(self):
        await begin_booking(self.message, self.context)
        await self.text("x" * 201)
        self.assertEqual(self.flow()["step"], "purpose")
        await self.text("<b>Study</b>")
        await self.text("friend@smu.edu.sg")
        await self.click("save")
        await self.text("x" * 41)
        self.assertEqual(self.flow()["step"], "save_name")
        await self.text("<Friend>")
        self.assertIn("&lt;b&gt;Study&lt;/b&gt;", self.last_text())
        await open_contacts(self.message, self.context)
        self.assertIn("&lt;Friend&gt;", self.last_text())

    async def test_contacts_and_partial_answers_survive_persistence_and_are_per_user(
        self,
    ):
        await self.purpose_and_email()
        save_contact(self.context, "Friend", "friend@smu.edu.sg")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.pickle"
            persistence = PicklePersistence(filepath=path)
            await persistence.update_user_data(7, self.context.user_data)
            await persistence.update_user_data(8, {})
            await persistence.flush()
            restored = await PicklePersistence(filepath=path).get_user_data()
        self.assertEqual(
            restored[7][CONTACTS_KEY], self.context.user_data[CONTACTS_KEY]
        )
        self.assertEqual(restored[7][FLOW_KEY]["step"], "save_choice")
        self.assertNotIn(CONTACTS_KEY, restored[8])

    async def test_validate_and_confirm_remain_distinct_explicit_actions(self):
        await self.purpose_and_email()
        await self.click("skip")
        self.backend.assert_not_awaited()
        token = next(iter(self.context.user_data["booking_drafts"]))
        status = Mock(edit_text=AsyncMock())
        self.message.reply_text.return_value = status
        query = Mock(
            message=self.message, answer=AsyncMock(), data=f"booking_validate:{token}"
        )
        await bot.booking_action_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertFalse(self.backend.await_args.kwargs["submit"])
        self.assertIn(token, self.context.user_data["booking_drafts"])
        query.data = f"booking_confirm:{token}"
        await bot.booking_action_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertTrue(self.backend.await_args.kwargs["submit"])
        self.assertNotIn(token, self.context.user_data["booking_drafts"])

    async def test_uncertain_submission_disables_review_instead_of_offering_retry(self):
        from bot.fbs_client import BookingOutcomeUnknown

        await self.purpose_and_email()
        await self.click("skip")
        token = next(iter(self.context.user_data["booking_drafts"]))
        self.backend.side_effect = BookingOutcomeUnknown(
            "Check FBS My Bookings before retrying."
        )
        status = Mock(edit_text=AsyncMock())
        self.message.reply_text.return_value = status
        query = Mock(
            message=self.message,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            data=f"booking_confirm:{token}",
        )
        update = SimpleNamespace(callback_query=query)
        await bot.booking_action_callback(update, self.context)
        self.assertNotIn(token, self.context.user_data["booking_drafts"])
        self.assertIn("disabled", status.edit_text.await_args.args[0])
        await bot.booking_action_callback(update, self.context)
        self.backend.assert_awaited_once()

    async def test_only_confirm_sends_receipt_and_never_persists_image_bytes(self):
        await self.purpose_and_email()
        await self.click("skip")
        token = next(iter(self.context.user_data["booking_drafts"]))
        self.backend.return_value = {
            "status": "verified",
            "reference_number": "BK-TEST",
            "receipt_png": b"png",
        }
        self.context.bot.send_photo = AsyncMock()
        query = Mock(
            message=self.message,
            from_user=SimpleNamespace(id=7),
            answer=AsyncMock(),
            data=f"booking_validate:{token}",
        )
        update = SimpleNamespace(callback_query=query)
        await bot.booking_action_callback(update, self.context)
        self.context.bot.send_photo.assert_not_awaited()
        query.data = f"booking_confirm:{token}"
        await bot.booking_action_callback(update, self.context)
        self.context.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.context.bot.send_photo.await_args.kwargs["chat_id"], 7)
        self.assertNotIn(token, self.context.user_data["booking_drafts"])
        self.assertNotIn("receipt_png", repr(self.context.user_data))

    async def test_no_room_selection_does_not_open_questions(self):
        self.context.user_data.pop("selected_room")
        await begin_booking(self.message, self.context)
        self.assertNotIn(FLOW_KEY, self.context.user_data)
        self.assertIn("choose one first", self.last_text())

    async def test_select_room_opens_questions_immediately(self):
        self.context.user_data["last_room_order"] = ["SCIS1 GSR 3-1"]
        query = Mock(message=self.message, answer=AsyncMock(), data="select_room:0")
        await bot.select_room_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertEqual(self.flow()["step"], "purpose")
