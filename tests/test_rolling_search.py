import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from telegram.error import BadRequest

from bot import bot
from bot.booking_flow import FLOW_KEY, begin_booking
from bot.fbs_client import (
    FBS_TIMEZONE,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
    search_config_from_bot,
)


class RollingSearchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 1, 13, 24, tzinfo=FBS_TIMEZONE)
        clock = patch("bot.fbs_client.datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.side_effect = lambda tz=None: self.now.astimezone(tz)
        self.addCleanup(clock.stop)
        self.status = Mock(edit_text=AsyncMock())
        self.message = Mock(
            chat_id=7, reply_text=AsyncMock(return_value=self.status), text=""
        )
        self.context = SimpleNamespace(user_data={}, bot=Mock(), args=[])

    def query(self, data):
        return Mock(
            message=self.message,
            from_user=SimpleNamespace(id=7),
            data=data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

    def old_search(self, start="11:00", end="13:30"):
        config = SearchConfig("01-Nov-2026", start, end)
        self.context.user_data.update(
            last_search=SearchResult(
                config,
                [RoomAvailability("Room A", [TimeSlot(start, end, "free")])],
                "test",
            ).to_dict(),
            last_room_order=["Room A"],
            selected_room="Room A",
        )
        return config

    def test_default_is_next_half_hour_for_two_hours_without_persisting_times(self):
        config = {}
        result = search_config_from_bot(config)
        self.assertEqual(
            (result.date, result.start_time, result.end_time),
            ("01-Nov-2026", "13:30", "15:30"),
        )
        self.assertEqual(config, {})
        self.now = self.now.replace(minute=31)
        result = search_config_from_bot(config)
        self.assertEqual((result.start_time, result.end_time), ("14:00", "16:00"))

    def test_exact_boundary_uses_next_future_half_hour(self):
        self.now = self.now.replace(minute=30, second=0)
        self.assertEqual(search_config_from_bot({}).start_time, "14:00")

    def test_late_window_advances_to_next_date_without_shortening_duration(self):
        self.now = self.now.replace(hour=22, minute=1)
        config = search_config_from_bot({})
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
        result = search_config_from_bot(config)
        self.assertEqual(
            (result.date, result.start_time, result.end_time),
            ("02-Nov-2026", "09:30", "11:00"),
        )
        self.now = self.now.replace(hour=18)
        self.assertEqual(search_config_from_bot(config), result)

    def test_menu_displays_resolved_rolling_start_and_two_hour_default(self):
        labels = [
            button.text
            for row in bot._build_scrape_menu_keyboard(self.context).inline_keyboard
            for button in row
        ]
        self.assertTrue(any("13:30 SGT (rolling)" in label for label in labels))
        self.assertIn("⏳ Duration: 2h", labels)
        self.assertIn("13:30–15:30 SGT", bot._search_settings_text(self.context))
        self.assertIn("13:30", bot.SCRAPE_PARAM_CHOICES["start_time"])

    async def test_reset_clears_fixed_choices_and_restores_rolling_default(self):
        self.context.user_data["scrape_config"] = {
            "start_time": "11:00",
            "duration_hrs": 2.5,
        }
        query = self.query("pick:reset")
        await bot.scrape_config_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertEqual(self.context.user_data["scrape_config"], {})
        self.assertIn("13:30–15:30", query.edit_message_text.await_args.args[0])

    async def test_identical_menu_edits_are_harmless_for_done_reset_and_selections(
        self,
    ):
        for data in (
            "pick:done",
            "pick:reset",
            "set:date:AUTO",
            "set:start_time:AUTO",
            "set:duration:2",
            "pick:start_time",
        ):
            query = self.query(data)
            query.edit_message_text.side_effect = BadRequest(
                "Message is not modified: specified new message content and reply markup are exactly the same"
            )
            with self.subTest(data=data):
                await bot.scrape_config_callback(
                    SimpleNamespace(callback_query=query), self.context
                )
                query.answer.assert_awaited_once()
                self.assertNotIn("start_time", self.context.user_data["scrape_config"])

    async def test_other_bad_requests_are_not_silently_swallowed(self):
        query = self.query("pick:done")
        query.edit_message_text.side_effect = BadRequest("Message can't be edited")
        with self.assertRaises(BadRequest):
            await bot.scrape_config_callback(
                SimpleNamespace(callback_query=query), self.context
            )

    async def test_expired_button_refreshes_menu_without_changing_preferences(self):
        self.context.user_data["scrape_config"] = {"floors": ["Level 2"]}
        query = self.query("pick:reset")
        query.answer.side_effect = BadRequest(
            "Query is too old and response timeout expired or query ID is invalid"
        )
        await bot.scrape_config_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertEqual(
            self.context.user_data["scrape_config"], {"floors": ["Level 2"]}
        )
        self.assertIn("request expired", self.message.reply_text.await_args.args[0])
        self.assertIn("13:30–15:30", self.message.reply_text.await_args.args[0])
        query.edit_message_text.assert_not_awaited()

    async def test_error_log_identifies_known_bad_requests_without_private_details(
        self,
    ):
        error = BadRequest("Can't parse entities: private booking details")
        with self.assertLogs(bot.logger, level="ERROR") as logs:
            await bot.telegram_error(None, SimpleNamespace(error=error))
        self.assertIn("message formatting was rejected", logs.output[0])
        self.assertNotIn("private booking details", logs.output[0])

    async def test_unknown_bad_request_logging_does_not_expose_details(self):
        with self.assertLogs(bot.logger, level="ERROR") as logs:
            await bot.telegram_error(
                None, SimpleNamespace(error=BadRequest("private details"))
            )
        self.assertIn("unrecognized request rejection", logs.output[0])
        self.assertNotIn("private details", logs.output[0])

    async def test_rolling_options_preserve_other_search_preferences(self):
        self.context.user_data["scrape_config"] = {
            "date_raw": "2 November 2026",
            "start_time": "11:00",
            "floors": ["Level 2"],
        }
        for data in ("set:date:AUTO", "set:start_time:AUTO"):
            query = self.query(data)
            await bot.scrape_config_callback(
                SimpleNamespace(callback_query=query), self.context
            )
        self.assertEqual(
            self.context.user_data["scrape_config"], {"floors": ["Level 2"]}
        )
        self.assertIn("13:30–15:30", query.edit_message_text.await_args.args[0])

    async def test_selecting_past_time_warns_immediately_in_settings(self):
        query = self.query("set:start_time:11:00")
        await bot.scrape_config_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertIn("already passed", query.edit_message_text.await_args.args[0])

    async def test_past_start_is_rejected_before_scraping_and_clears_stale_results(
        self,
    ):
        self.old_search()
        self.context.user_data["scrape_config"] = {
            "date_raw": "1 November 2026",
            "start_time": "11:00",
        }
        self.context.user_data["saved_cobookers"] = {
            "friend": {"name": "Friend", "email": "friend@smu.edu.sg"}
        }
        with patch.object(bot, "search_fbs", AsyncMock()) as backend:
            await bot.run_script(self.query("run_script"), self.context)
        backend.assert_not_awaited()
        self.assertIn("already passed", self.message.reply_text.await_args.args[0])
        self.assertNotIn("last_search", self.context.user_data)
        self.assertNotIn("selected_room", self.context.user_data)
        self.assertIn("saved_cobookers", self.context.user_data)
        self.assertIn("already passed", bot._search_settings_text(self.context))

    async def test_expiry_during_search_never_presents_bookable_results(self):
        config = SearchConfig("01-Nov-2026", "13:30", "15:30")
        result = SearchResult(
            config,
            [RoomAvailability("Room A", [TimeSlot("13:30", "15:30", "free")])],
            "test",
        )

        async def finish(*args, **kwargs):
            self.now = self.now.replace(minute=30)
            return result

        with patch.object(bot, "search_fbs", AsyncMock(side_effect=finish)):
            await bot.run_script(self.query("run_script"), self.context)
        self.assertNotIn("last_search", self.context.user_data)
        self.assertIn("Search ended", self.status.edit_text.await_args.args[0])

    async def test_old_room_button_and_auto_pick_do_not_start_questions(self):
        for handler, data in (
            (bot.room_details_callback, "room:0"),
            (bot.select_room_callback, "select_room:0"),
            (bot.autopick_callback, "autopick"),
        ):
            self.old_search()
            await handler(
                SimpleNamespace(callback_query=self.query(data)), self.context
            )
            self.assertNotIn(FLOW_KEY, self.context.user_data)
            self.assertIn("already passed", self.message.reply_text.await_args.args[0])

    async def test_booking_stops_mid_question_when_start_time_passes(self):
        self.old_search("13:30", "15:30")
        await begin_booking(self.message, self.context)
        self.assertIn(FLOW_KEY, self.context.user_data)
        self.now = self.now.replace(minute=30)
        self.message.text = "Project meeting"
        await bot.handle_text_input(SimpleNamespace(message=self.message), self.context)
        self.assertNotIn(FLOW_KEY, self.context.user_data)
        self.assertNotIn("booking_drafts", self.context.user_data)
        self.assertIn("already passed", self.message.reply_text.await_args.args[0])

    async def test_wizard_button_also_rechecks_time_before_asking_next_question(self):
        self.old_search("13:30", "15:30")
        await begin_booking(self.message, self.context)
        self.message.text = "Project meeting"
        await bot.handle_text_input(SimpleNamespace(message=self.message), self.context)
        token = self.context.user_data[FLOW_KEY]["id"]
        self.now = self.now.replace(minute=30)
        query = self.query(f"wizard:{token}:new")
        await bot.wizard_callback(SimpleNamespace(callback_query=query), self.context)
        self.assertNotIn(FLOW_KEY, self.context.user_data)
        self.assertIn("already passed", self.message.reply_text.await_args.args[0])

    async def test_legacy_booking_cannot_create_review_for_past_window(self):
        self.old_search()
        self.context.args = ["Meeting", "|", "friend@smu.edu.sg"]
        await bot.book_command(SimpleNamespace(message=self.message), self.context)
        self.assertNotIn("booking_drafts", self.context.user_data)
        self.assertIn("already passed", self.message.reply_text.await_args.args[0])
