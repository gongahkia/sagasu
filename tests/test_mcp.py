"""MCP lifecycle/protocol and write safety, using a fake backend (never live FBS)."""

import asyncio
import base64
import copy
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bot.fbs_client import AuthenticationRequired, BookingOutcomeUnknown
from bot.mcp_service import AgentError, AgentService, Backend
from bot.fbs_tasks import TaskApprovalOutcomeUnknown

try:
    from mcp import Client, StdioServerParameters
    from mcp_types import ElicitResult
    from bot.mcp_server import create_server, serve
except ImportError:
    Client = None


CONFIG = {"date": "01-Nov-2099", "start_time": "14:00", "end_time": "16:00"}
ROOM = {
    "name": "SCIS1 GSR 2-1",
    "timeslots": [
        {"start": "14:00", "end": "16:00", "status": "free", "details": None}
    ],
}
ROW = {
    "reference_number": "BK-20991101-000143",
    "room": ROOM["name"],
    "date_time": "01-Nov-2099 (Sun) 14:00 - 16:00 (2hrs)",
    "fbs_status": "Confirmed",
    "can_cancel": True,
}
PNG = b"\x89PNG\r\n\x1a\nexample-receipt"
TASK = {
    "reference_number": ROW["reference_number"],
    "room": ROW["room"],
    "date_time": ROW["date_time"],
    "request_type": "Booking Approval",
    "requested_by": "A student",
    "status": "Pending Approval",
    "can_approve": True,
    "can_view": True,
}


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.error = None
        self.mfa = False
        self.tasks = []

    async def run(self, kind, payload, progress, notify):
        self.calls.append((kind, payload))
        await progress("Checking calendar")
        if self.mfa:
            await notify("Approve this sign-in using 88")
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        if kind == "search":
            return {
                "config": asdict(payload),
                "rooms": [copy.deepcopy(ROOM)],
                "scraped_at": "2099-11-01T12:00:00+08:00",
            }
        if kind == "bookings":
            return {"bookings": [copy.deepcopy(ROW)], "total": 1, "complete": True}
        if kind == "tasks":
            return {
                "tasks": copy.deepcopy(self.tasks),
                "total": len(self.tasks),
                "complete": True,
            }
        if kind in {"approve_task", "task_details"}:
            return {
                "status": "approved" if kind == "approve_task" else "details",
                "task": copy.deepcopy(payload),
                "reference_number": payload["reference_number"],
                "task_status": (
                    "Approved" if kind == "approve_task" else payload["status"]
                ),
                "receipt_png": PNG,
                "receipt_kind": "booking_details",
                "booking_status_verified": False,
            }
        if kind in {"book", "cancel"}:
            return {
                "status": "confirmed" if kind == "book" else "cancelled",
                "reference_number": ROW["reference_number"],
                "fbs_status": "Pending Confirmation" if kind == "book" else "Cancelled",
                "receipt_png": PNG,
            }
        return {"status": "validated" if kind == "validate" else "authenticated"}


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend()
        self.now = 1000
        self.service = AgentService(backend=self.backend, clock=lambda: self.now)
        self.addAsyncCleanup(self.service.close)

    async def finish(self, job):
        await self.service.get(job["operation_id"]).task
        return self.service.get(job["operation_id"])

    async def review(self):
        job = await self.finish(self.service.search(CONFIG))
        return self.service.review_booking(
            job.id, ROOM["name"], "Project meeting", "friend@scis.smu.edu.sg"
        )


class ServiceTests(ServiceCase):
    async def task_review(self):
        self.backend.tasks = [TASK]
        job = await self.finish(self.service.start("tasks"))
        return self.service.review_task_approval(job.id, TASK["reference_number"])

    async def test_task_approval_review_and_one_shot_submission(self):
        review = await self.task_review()
        self.assertEqual(self.backend.calls[-1][0], "tasks")
        review["summary"]["task"]["room"] = "tampered"
        job = await self.finish(self.service.confirm(review["review_id"]))
        self.assertEqual(self.backend.calls[-1], ("approve_task", TASK))
        self.assertEqual(job.receipt, PNG)
        self.assertFalse(job.result["booking_status_verified"])
        with self.assertRaises(AgentError):
            self.service.confirm(review["review_id"])

    async def test_task_review_rejects_unlisted_duplicate_or_unactionable_tasks(self):
        for rows, reference in (
            ([TASK], "missing"),
            ([TASK, TASK], TASK["reference_number"]),
            ([{**TASK, "can_approve": False}], TASK["reference_number"]),
        ):
            self.backend.tasks = rows
            job = await self.finish(self.service.start("tasks"))
            with self.assertRaises(AgentError):
                self.service.review_task_approval(job.id, reference)

    async def test_task_approval_unknown_is_not_retried(self):
        review = await self.task_review()
        self.backend.error = TaskApprovalOutcomeUnknown("private browser content")
        job = await self.finish(self.service.confirm(review["review_id"]))
        self.assertEqual(job.status, "outcome_unknown")
        self.assertNotIn("private browser content", json.dumps(job.public()))
        with self.assertRaises(AgentError):
            self.service.confirm(review["review_id"])

    async def test_running_task_approval_cannot_be_interrupted(self):
        review = await self.task_review()
        self.backend.gate = asyncio.Event()
        job = self.service.confirm(review["review_id"])
        await asyncio.sleep(0)
        with self.assertRaises(AgentError):
            await self.service.cancel(job["operation_id"])
        self.backend.gate.set()
        await self.finish(job)

    async def test_task_details_is_readonly_and_returns_separate_image(self):
        self.backend.tasks = [TASK]
        source = await self.finish(self.service.start("tasks"))
        job = await self.finish(
            self.service.task_details(source.id, TASK["reference_number"])
        )
        self.assertEqual(job.kind, "task_details")
        self.assertEqual(job.receipt, PNG)
        self.assertNotIn("approve_task", [kind for kind, _ in self.backend.calls])

    async def test_search_is_background_and_results_paginate(self):
        self.backend.gate = asyncio.Event()
        operation = self.service.search(CONFIG)
        self.assertEqual(operation["status"], "queued")
        await asyncio.sleep(0)
        self.assertEqual(self.service.get(operation["operation_id"]).status, "running")
        self.backend.gate.set()
        job = await self.finish(operation)
        job.result["rooms"] *= 25
        page = self.service.result(job.id, 20, 5)["result"]
        self.assertEqual(len(page["rooms"]), 5)
        self.assertIsNone(page["pagination"]["next_offset"])
        self.assertEqual(len(job.result["rooms"]), 25)
        with self.assertRaises(AgentError):
            self.service.result(job.id, -1)

    async def test_default_uses_rolling_bot_config(self):
        with patch("bot.mcp_service.search_config_from_bot") as default:
            from bot.fbs_client import SearchConfig

            default.return_value = SearchConfig.from_mapping(CONFIG)
            await self.finish(self.service.search())
            default.assert_called_once_with({})

    async def test_search_rejects_past_or_invalid_times(self):
        for change in (
            {"date": "01-Jan-2000"},
            {"start_time": "14:01"},
            {"end_time": "13:30"},
            {"date": "2026-11-01"},
        ):
            with self.assertRaises(AgentError):
                self.service.search({**CONFIG, **change})
        self.assertEqual(self.backend.calls, [])

    async def test_review_is_immutable_and_does_not_write(self):
        review = await self.review()
        review["summary"]["purpose"] = "tampered"
        self.assertEqual(
            self.service.review(review["review_id"])["summary"]["purpose"],
            "Project meeting",
        )
        self.assertEqual([kind for kind, _ in self.backend.calls], ["search"])

    async def test_review_rejects_unknown_or_blocked_room_and_bad_email(self):
        job = await self.finish(self.service.search(CONFIG))
        for room, purpose, email in (
            ("invented", "Meeting", "a@smu.edu.sg"),
            (ROOM["name"], "", "a@smu.edu.sg"),
            (ROOM["name"], "Meeting", "a@example.com"),
        ):
            with self.assertRaises(AgentError):
                self.service.review_booking(job.id, room, purpose, email)
        job.result["rooms"][0]["timeslots"][0]["status"] = "booked"
        with self.assertRaises(AgentError):
            self.service.review_booking(job.id, ROOM["name"], "Meeting", "a@smu.edu.sg")

    async def test_validation_does_not_consume_review_or_submit(self):
        review = await self.review()
        await self.finish(self.service.validate(review["review_id"]))
        self.assertEqual(self.backend.calls[-1][0], "validate")
        self.assertEqual(self.service.review(review["review_id"]), review)

    async def test_confirm_is_one_shot_and_receipt_separate(self):
        review = await self.review()
        operation = self.service.confirm(review["review_id"])
        with self.assertRaises(AgentError):
            self.service.confirm(review["review_id"])
        job = await self.finish(operation)
        self.assertEqual(job.receipt, PNG)
        self.assertNotIn("receipt_png", self.service.result(job.id)["result"])
        self.assertEqual(job.result["fbs_status"], "Pending Confirmation")
        self.assertEqual([kind for kind, _ in self.backend.calls].count("book"), 1)

    async def test_review_expiry_and_discard_prevent_submission(self):
        review = await self.review()
        self.now += 601
        with self.assertRaises(AgentError):
            self.service.confirm(review["review_id"])
        review = await self.review()
        self.service.discard(review["review_id"])
        with self.assertRaises(AgentError):
            self.service.confirm(review["review_id"])

    async def test_cancellation_requires_exact_cancellable_snapshot(self):
        job = await self.finish(self.service.start("bookings"))
        with self.assertRaises(AgentError):
            self.service.review_cancellation(job.id, "not-listed", "No longer needed")
        review = self.service.review_cancellation(
            job.id, ROW["reference_number"], "No longer needed"
        )
        self.assertEqual(self.backend.calls[-1][0], "bookings")
        await self.finish(self.service.confirm(review["review_id"]))
        self.assertEqual(self.backend.calls[-1], ("cancel", (ROW, "No longer needed")))
        job.result["bookings"][0]["can_cancel"] = False
        with self.assertRaises(AgentError):
            self.service.review_cancellation(
                job.id, ROW["reference_number"], "No longer needed"
            )

    async def test_unknown_write_is_never_replayed(self):
        review = await self.review()
        self.backend.error = BookingOutcomeUnknown("secret-private-details")
        job = await self.finish(self.service.confirm(review["review_id"]))
        self.assertEqual(job.status, "outcome_unknown")
        self.assertNotIn("secret-private-details", json.dumps(job.public()))
        with self.assertRaises(AgentError):
            self.service.confirm(review["review_id"])

    async def test_unexpected_write_error_is_uncertain_and_raw_values_are_private(self):
        review = await self.review()
        self.backend.error = RuntimeError("password-cookie-must-not-escape")
        job = await self.finish(self.service.confirm(review["review_id"]))
        self.assertEqual(job.status, "outcome_unknown")
        self.assertNotIn("password-cookie", json.dumps(self.service.result(job.id)))

    async def test_shutdown_waits_for_running_write(self):
        review = await self.review()
        self.backend.gate = asyncio.Event()
        operation = self.service.confirm(review["review_id"])
        await asyncio.sleep(0)
        closing = asyncio.create_task(self.service.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        self.backend.gate.set()
        await closing
        self.assertEqual(
            self.service.get(operation["operation_id"]).status, "succeeded"
        )

    async def test_oversized_receipt_does_not_change_verified_outcome(self):
        review = await self.review()
        original = self.backend.run

        async def oversized(*args):
            result = await original(*args)
            result["receipt_png"] = b"x" * (4 * 1024 * 1024 + 1)
            return result

        self.backend.run = oversized
        job = await self.finish(self.service.confirm(review["review_id"]))
        self.assertEqual(job.status, "succeeded")
        self.assertFalse(job.public()["receipt_available"])
        self.assertIn("receipt_error", job.result)

    async def test_mfa_status_does_not_expose_number(self):
        self.backend.mfa = True
        self.backend.gate = asyncio.Event()
        notify = AsyncMock()
        self.service.notify = notify
        operation = self.service.start("login")
        await asyncio.sleep(0)
        job = self.service.get(operation["operation_id"])
        self.assertEqual(job.status, "awaiting_mfa")
        self.assertNotIn("88", json.dumps(job.public()))
        notify.assert_awaited_once_with("Approve this sign-in using 88")
        await self.service.cancel(job.id)

    async def test_missing_notifier_fails_safely(self):
        self.backend.mfa = True
        job = await self.finish(self.service.start("login"))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error["code"], "AuthenticationRequired")

    async def test_reads_can_stop_but_active_writes_cannot(self):
        review = await self.review()
        self.backend.gate = asyncio.Event()
        read = self.service.start("tasks")
        await asyncio.sleep(0)
        queued_write = self.service.confirm(review["review_id"])
        self.assertEqual(
            (await self.service.cancel(queued_write["operation_id"]))["status"],
            "cancelled",
        )
        self.assertEqual(
            (await self.service.cancel(read["operation_id"]))["status"], "cancelled"
        )
        self.backend.gate.set()
        review = await self.review()
        self.backend.gate.clear()
        write = self.service.confirm(review["review_id"])
        await asyncio.sleep(0)
        with self.assertRaises(AgentError):
            await self.service.cancel(write["operation_id"])
        self.backend.gate.set()
        await self.finish(write)

    async def test_bounded_retention_and_expiry(self):
        self.service.limit = 2
        first = await self.finish(self.service.start("tasks"))
        await self.finish(self.service.start("tasks"))
        await self.finish(self.service.start("tasks"))
        self.assertEqual(len(self.service.jobs), 2)
        with self.assertRaises(AgentError):
            self.service.get(first.id)
        self.now += 3601
        self.service.prune()
        self.assertEqual(self.service.jobs, {})

    async def test_queue_backpressure_and_shutdown(self):
        self.service.limit = 1
        self.backend.gate = asyncio.Event()
        operation = self.service.start("tasks")
        with self.assertRaises(AgentError):
            self.service.start("tasks")
        await self.service.close()
        self.assertTrue(self.service.get(operation["operation_id"]).task.done())
        with self.assertRaises(AgentError):
            self.service.start("tasks")


@unittest.skipIf(Client is None, "Install .[mcp] for MCP protocol tests")
class ProtocolTests(ServiceCase):
    def enable_personal_data(self):
        from bot.personal import PersonalStore

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service.profile = PersonalStore(
            Path(temporary.name) / "personal.sqlite3", "telegram:42"
        )

    async def test_personal_tools_roundtrip_and_persist_across_services(self):
        from bot.personal import PersonalStore

        self.enable_personal_data()
        async with Client(create_server(self.service)) as client:
            person = await self.call(
                client, "save_cobooker", name="Friend", email="Friend@smu.edu.sg"
            )
            listed = await self.call(client, "list_cobookers")
            self.assertEqual(listed["cobookers"][0]["id"], person["id"])
            await self.call(
                client,
                "set_search_preferences",
                preferences={"floors": ["Level 3"], "duration_hrs": 3},
            )
            preferences = await self.call(client, "get_search_preferences")
            self.assertEqual(preferences["effective_search"]["floors"], ["Level 3"])
            reopened = PersonalStore(
                self.service.profile.path, self.service.profile.scope
            )
            self.assertIn(person["id"], reopened.contacts())
            self.assertEqual(reopened.preferences()["duration_hrs"], 3)
            await self.call(
                client,
                "save_cobooker",
                name="Renamed",
                email="new@smu.edu.sg",
                cobooker_id=person["id"],
            )
            self.assertEqual(reopened.resolve_contact(person["id"]), "new@smu.edu.sg")
            await self.call(client, "reset_search_preferences")
            self.assertEqual(reopened.preferences(), {})
            self.assertIn(person["id"], reopened.contacts())
            await self.call(client, "delete_cobooker", cobooker_id=person["id"])
            self.assertEqual(reopened.contacts(), {})
        self.assertEqual(self.backend.calls, [])

    async def test_preference_patch_only_changes_supplied_fields_and_null_restores_rolling(
        self,
    ):
        self.enable_personal_data()
        async with Client(create_server(self.service)) as client:
            await self.call(
                client,
                "set_search_preferences",
                preferences={
                    "floors": ["Level 2"],
                    "start_time": "09:00",
                    "duration_hrs": 3,
                },
            )
            await self.call(
                client,
                "set_search_preferences",
                preferences={"room_capacity": "LessThan5Pax"},
            )
            result = await self.call(
                client,
                "set_search_preferences",
                preferences={"start_time": None, "date_raw": None},
            )
            self.assertTrue(result["rolling_start"])
            self.assertEqual(
                result["preferences"],
                {
                    "floors": ["Level 2"],
                    "duration_hrs": 3,
                    "room_capacity": "LessThan5Pax",
                },
            )
            options = await self.call(client, "search_options")
            self.assertIn("Level 2", options["choices"]["floor"])

    async def test_invalid_personal_input_never_changes_data_or_contacts_fbs(self):
        self.enable_personal_data()
        async with Client(create_server(self.service)) as client:
            for name, arguments in (
                ("save_cobooker", {"name": "Friend", "email": "private@example.com"}),
                (
                    "set_search_preferences",
                    {"preferences": {"floors": ["unrecognized-floor"]}},
                ),
                (
                    "set_search_preferences",
                    {"preferences": {"password": "do-not-store"}},
                ),
            ):
                response = await client.call_tool(name, arguments)
                self.assertTrue(response.is_error)
            self.assertEqual(self.service.profile.contacts(), {})
            self.assertEqual(self.service.profile.preferences(), {})
        self.assertEqual(self.backend.calls, [])

    async def test_task_approval_elicitation_and_generic_receipt(self):
        self.backend.tasks = [TASK]
        prompts = []

        async def approve(context, params):
            prompts.append(params.message)
            return ElicitResult(action="accept", content={"confirm": True})

        async with Client(
            create_server(self.service), elicitation_callback=approve
        ) as client:
            source = await self.call(client, "list_tasks")
            await self.finish(source)
            review = await self.call(
                client,
                "task_approval_review",
                tasks_operation_id=source["operation_id"],
                reference_number=TASK["reference_number"],
            )
            result = await self.call(
                client, "confirm_review", review_id=review["review_id"]
            )
            job = await self.finish(result)
            receipt = await client.call_tool(
                "operation_receipt", {"operation_id": job.id}
            )
            self.assertFalse(receipt.is_error)
            self.assertEqual(base64.b64decode(receipt.content[0].data), PNG)
            self.assertIn("Booking Approval", prompts[0])
            self.assertIn("approve_task", prompts[0])
            self.assertEqual(job.result["status"], "approved")

    async def call(self, client, tool_name, **arguments):
        result = await client.call_tool(tool_name, arguments)
        if result.is_error:
            return result
        self.assertIsNotNone(result.structured_content)
        return result.structured_content

    async def test_tool_schema_has_no_model_supplied_approval(self):
        async with Client(create_server(self.service)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(
                set(tools["confirm_review"].input_schema["properties"]), {"review_id"}
            )
            self.assertTrue(tools["confirm_review"].annotations.destructive_hint)
            status = await self.call(client, "server_status")
            self.assertFalse(status["mfa_notifications"])
            result = await client.call_tool(
                "search_rooms", {"config": {**CONFIG, "password": "do-not-accept"}}
            )
            self.assertTrue(result.is_error)
            self.assertEqual(self.backend.calls, [])

    async def test_real_protocol_search_review_and_receipt(self):
        seen = []

        async def approve(context, params):
            seen.append(params.message)
            return ElicitResult(action="accept", content={"confirm": True})

        async with Client(
            create_server(self.service), elicitation_callback=approve
        ) as client:
            operation = await self.call(client, "search_rooms", config=CONFIG)
            await self.finish(operation)
            review = await self.call(
                client,
                "booking_review",
                search_operation_id=operation["operation_id"],
                room=ROOM["name"],
                purpose="Project meeting",
                co_booker="friend@scis.smu.edu.sg",
            )
            confirmed = await self.call(
                client, "confirm_review", review_id=review["review_id"]
            )
            job = await self.finish(confirmed)
            self.assertEqual(len(seen), 1)
            self.assertIn("Project meeting", seen[0])
            result = await client.call_tool("booking_receipt", {"operation_id": job.id})
            self.assertFalse(result.is_error)
            self.assertEqual(base64.b64decode(result.content[0].data), PNG)
            duplicate = await client.call_tool(
                "confirm_review", {"review_id": review["review_id"]}
            )
            self.assertTrue(duplicate.is_error)

    async def test_legacy_jsonrpc_confirmation_and_cancellation(self):
        asked = []

        async def approve(context, params):
            asked.append(params.message)
            return ElicitResult(action="accept", content={"confirm": True})

        async with Client(
            create_server(self.service), mode="legacy", elicitation_callback=approve
        ) as client:
            operation = await self.call(client, "list_bookings")
            await self.finish(operation)
            review = await self.call(
                client,
                "cancellation_review",
                bookings_operation_id=operation["operation_id"],
                reference_number=ROW["reference_number"],
                reason="Plans changed",
            )
            submitted = await self.call(
                client, "confirm_review", review_id=review["review_id"]
            )
            job = await self.finish(submitted)
            self.assertEqual(job.result["fbs_status"], "Cancelled")
            self.assertIn(ROW["reference_number"], asked[0])

    async def test_expiry_while_user_is_answering_prevents_write(self):
        review = await self.review()

        async def late_approval(context, params):
            self.now += 601
            return ElicitResult(action="accept", content={"confirm": True})

        async with Client(
            create_server(self.service), elicitation_callback=late_approval
        ) as client:
            result = await client.call_tool(
                "confirm_review", {"review_id": review["review_id"]}
            )
            self.assertTrue(result.is_error)
        self.assertNotIn("book", [kind for kind, _ in self.backend.calls])

    async def test_protocol_decline_false_and_missing_elicitation_do_not_write(self):
        for response in (
            None,
            {"action": "decline"},
            {"action": "cancel"},
            {"action": "accept", "content": {"confirm": False}},
        ):
            review = await self.review()

            async def answer(context, params):
                return ElicitResult(**response)

            async with Client(
                create_server(self.service),
                elicitation_callback=answer if response else None,
            ) as client:
                result = await client.call_tool(
                    "confirm_review", {"review_id": review["review_id"]}
                )
                if response is not None:
                    self.assertFalse(result.is_error)
                    self.assertFalse(result.structured_content["submitted"])
                else:
                    self.assertTrue(result.is_error)
                    self.assertIn("cannot display", result.content[0].text)
            self.assertIn(review["review_id"], self.service.reviews)
        self.assertNotIn("book", [kind for kind, _ in self.backend.calls])

    async def test_subprocess_stdio_has_only_protocol_output_and_no_credentials_needed(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SAGASU_", "SMU_", "TELEGRAM_"))
                and key != "BOT_TOKEN"
            }
            env.update(
                SAGASU_BROWSER_PROFILE=str(path / "browser"),
                BOT_TOKEN="",
                TELEGRAM_OWNER_ID="",
                SMU_LOGIN_PASSWORD="",
                SMU_LOGIN_EMAIL="",
            )
            env_file = path / ".env"
            env_file.write_text("")
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "bot", "mcp", "--env-file", str(env_file)],
                env=env,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            async with Client(params) as client:
                status = await self.call(client, "server_status")
                self.assertEqual(status["transport"], "stdio")
                self.assertEqual(status["operations"], [])
                self.assertFalse(status["mfa_notifications"])


@unittest.skipIf(Client is None, "Install .[mcp] for MCP runtime tests")
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_mode_owns_profile_and_closes_both_adapters(self):
        from bot.config import Settings
        from bot.runtime import AlreadyRunning, InstanceLock

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            env = Path(directory) / ".env"
            env.write_text(
                "BOT_TOKEN=123:abcdefghijklmnopqrstuvwx\nTELEGRAM_OWNER_ID=42\nSAGASU_BROWSER_PROFILE=browser\nSAGASU_STATE_PATH=state.pickle\n"
            )
            settings = Settings.load(env)
            app = Mock()
            app.running = False
            app.updater.running = False
            app.__aenter__ = AsyncMock(return_value=app)
            app.__aexit__ = AsyncMock()

            async def start():
                app.running = True

            async def start_polling(**kwargs):
                app.updater.running = True

            async def stop():
                app.running = False

            async def stop_polling():
                app.updater.running = False

            app.start = AsyncMock(side_effect=start)
            app.stop = AsyncMock(side_effect=stop)
            app.updater.start_polling = AsyncMock(side_effect=start_polling)
            app.updater.stop = AsyncMock(side_effect=stop_polling)

            def server_factory(service, *, lifespan):
                async def run():
                    async with lifespan(None):
                        self.assertTrue(app.running)
                        self.assertIsNotNone(service.notify)
                        with self.assertRaises(AlreadyRunning):
                            with InstanceLock(settings.profile):
                                pass

                return Mock(run_stdio_async=AsyncMock(side_effect=run))

            with (
                patch("bot.mcp_server.create_server", side_effect=server_factory),
                patch("bot.bot.build_application", return_value=app),
                patch(
                    "bot.mcp_server.close_shared_browser", new_callable=AsyncMock
                ) as close,
            ):
                await serve(settings, with_telegram=True)
                close.assert_awaited_once()
            app.start.assert_awaited_once()
            app.stop.assert_awaited_once()
            app.updater.stop.assert_awaited_once()
            app.__aexit__.assert_awaited_once()
            with InstanceLock(settings.profile):
                pass

    async def test_locked_profile_fails_before_starting_server(self):
        from bot.config import Settings
        from bot.runtime import AlreadyRunning, InstanceLock

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            env = Path(directory) / ".env"
            env.write_text("SAGASU_BROWSER_PROFILE=browser\n")
            settings = Settings.load(env)
            server = Mock(run_stdio_async=AsyncMock())
            with (
                InstanceLock(settings.profile),
                patch("bot.mcp_server.create_server", return_value=server),
            ):
                with self.assertRaises(AlreadyRunning):
                    await serve(settings)
            server.run_stdio_async.assert_not_awaited()


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_and_book_explicitly_select_submit_flag(self):
        execute = AsyncMock(return_value={"status": "ok"})
        backend = Backend()
        with patch("bot.mcp_service.execute_booking", execute):
            await backend.run("validate", "request", None, None)
            self.assertFalse(execute.await_args.kwargs["submit"])
            await backend.run("book", "request", None, None)
            self.assertTrue(execute.await_args.kwargs["submit"])
