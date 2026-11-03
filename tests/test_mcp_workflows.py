"""Agent workflows over the real MCP protocol with a fake FBS backend."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sagasu.config import ConfigError, Settings
from sagasu.mcp_service import AgentError
from sagasu.personal import PersonalStore
from tests.test_mcp import Client, CONFIG, ROOM, ROW, TASK, ServiceCase

if Client is not None:
    from sagasu.mcp_server import create_server


class WorkflowServiceTests(ServiceCase):
    async def test_client_pagination_does_not_claim_partial_fbs_listing_is_complete(
        self,
    ):
        job = await self.finish(self.service.start("bookings"))
        job.result.update(
            total=50, complete=False, coverage_reason="unsupported_or_missing_next_page"
        )
        result = self.service.result(job.id)["result"]
        self.assertEqual(result["pagination"]["source_total"], 50)
        self.assertFalse(result["pagination"]["source_complete"])
        self.assertFalse(result["complete"])

    async def test_wait_returns_for_mfa_and_prompt_clears_on_completion(self):
        self.backend.mfa = True
        self.backend.gate = asyncio.Event()
        operation = self.service.start("login")
        result = await asyncio.wait_for(
            self.service.wait(operation["operation_id"], timeout=30), timeout=1
        )
        self.assertEqual(result["status"], "awaiting_mfa")
        self.assertEqual(result["next_tool"], "authentication_status")
        self.assertNotIn("88", json.dumps(result))
        self.assertIn(
            "88",
            self.service.authentication_status(operation["operation_id"])["prompt"],
        )
        self.backend.gate.set()
        await self.finish(operation)
        self.assertIsNone(
            self.service.authentication_status(operation["operation_id"])["prompt"]
        )

    async def test_cancelling_wait_for_auth_does_not_cancel_login(self):
        self.backend.gate = asyncio.Event()
        operation = self.service.start("login")
        waiter = asyncio.create_task(
            self.service.wait(operation["operation_id"], timeout=30)
        )
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        self.assertFalse(self.service.get(operation["operation_id"]).task.cancelled())
        self.backend.gate.set()
        await self.finish(operation)

    async def test_wait_timeout_and_cancelled_wait_do_not_cancel_job(self):
        self.backend.gate = asyncio.Event()
        operation = self.service.search(CONFIG)
        pending = await self.service.wait(operation["operation_id"], timeout=0.001)
        self.assertFalse(pending["terminal"])
        waiter = asyncio.create_task(self.service.wait(operation["operation_id"]))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        self.assertFalse(self.service.get(operation["operation_id"]).task.cancelled())
        self.backend.gate.set()
        result = await self.service.wait(operation["operation_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["rooms"], [ROOM])
        self.assertEqual(len(self.backend.calls), 1)

    async def test_wait_rejects_invalid_timeout_before_work(self):
        for timeout in (-1, 31, float("nan"), float("inf"), True):
            with self.assertRaises(AgentError):
                await self.service.wait("unknown", timeout=timeout)

    async def test_available_filters_before_pagination_and_preserves_snapshot(self):
        job = await self.finish(self.service.search(CONFIG))
        blocked = {
            "name": "Blocked room",
            "timeslots": [{"start": "14:00", "end": "16:00", "status": "booked"}],
        }
        job.result["rooms"] = [blocked, ROOM, {**ROOM, "name": "Another room"}]
        result = self.service.available_rooms(job.id, limit=1)
        self.assertEqual(result["available_total"], 2)
        self.assertEqual(result["searched_total"], 3)
        self.assertEqual(result["rooms"][0]["name"], "Another room")
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(
            self.service.available_rooms(job.id, 1)["rooms"][0]["name"], ROOM["name"]
        )
        self.assertEqual(len(job.result["rooms"]), 3)

    async def test_agent_submit_replays_success_and_unknown_without_second_write(self):
        from sagasu.fbs_client import BookingOutcomeUnknown

        for error in (None, BookingOutcomeUnknown("private details")):
            review = await self.review()
            self.backend.error = error
            operation = self.service.submit(review["review_id"])
            replay = self.service.submit(review["review_id"])
            self.assertEqual(operation["operation_id"], replay["operation_id"])
            job = await self.finish(operation)
            replay = self.service.submit(review["review_id"])
            self.assertEqual(replay["status"], job.status)
            self.assertEqual(replay["operation_id"], job.id)
            self.backend.error = None
        self.assertEqual([kind for kind, _ in self.backend.calls].count("book"), 2)

    async def test_expired_or_discarded_review_cannot_be_agent_submitted(self):
        review = await self.review()
        self.now += 601
        with self.assertRaises(AgentError):
            self.service.submit(review["review_id"])
        review = await self.review()
        self.service.discard(review["review_id"])
        with self.assertRaises(AgentError):
            self.service.submit(review["review_id"])
        self.assertNotIn("book", [kind for kind, _ in self.backend.calls])


@unittest.skipIf(Client is None, "Install Sagasu for MCP protocol tests")
class WorkflowProtocolTests(ServiceCase):
    async def test_actionable_failure_survives_mcp_without_raw_exception_text(self):
        from sagasu.fbs_client import AuthenticationRequired, BookingUnavailable

        async with Client(
            create_server(self.service, allow_agent_submission=True)
        ) as client:
            for reason, error in [
                (
                    "missing_password",
                    AuthenticationRequired(
                        "PRIVATE PASSWORD", reason="missing_password"
                    ),
                ),
                ("session_expired", AuthenticationRequired("PRIVATE ACCOUNT")),
                (
                    "outside_booking_horizon",
                    BookingUnavailable(
                        "PRIVATE PAGE TEXT",
                        reason="outside_booking_horizon",
                        details={
                            "checked_rooms": 7,
                            "available_total": 0,
                            "password": "PRIVATE PASSWORD",
                        },
                    ),
                ),
            ]:
                review = await self.review()
                self.backend.error = error
                operation = await self.call(
                    client, "validate_booking", review_id=review["review_id"]
                )
                status = await self.call(
                    client, "wait_operation", operation_id=operation["operation_id"]
                )
                self.assertEqual(status["error"]["reason"], reason)
                self.assertNotIn("PRIVATE", json.dumps(status))
                if reason == "outside_booking_horizon":
                    self.assertEqual(status["error"]["details"]["checked_rooms"], 7)
                    self.assertIsNone(status["error"]["next_tool"])
                else:
                    self.assertEqual(status["error"]["next_tool"], "login")
                self.backend.error = None

    async def test_mfa_prompt_is_available_privately_without_external_notifier(self):
        self.backend.mfa = True
        self.backend.gate = asyncio.Event()
        async with Client(
            create_server(self.service, allow_agent_submission=True)
        ) as client:
            operation = await self.call(client, "login")
            status = await self.call(
                client, "wait_operation", operation_id=operation["operation_id"]
            )
            self.assertEqual(status["status"], "awaiting_mfa")
            self.assertNotIn("88", json.dumps(status))
            prompt = await self.call(
                client, "authentication_status", operation_id=operation["operation_id"]
            )
            self.assertIn("88", prompt["prompt"])
            self.backend.gate.set()
            await self.finish(operation)
            prompt = await self.call(
                client, "authentication_status", operation_id=operation["operation_id"]
            )
            self.assertIsNone(prompt["prompt"])

    async def call(self, client, name, **arguments):
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def test_agent_client_without_forms_can_submit_each_write_kind_once(self):
        self.backend.tasks = [TASK]
        async with Client(
            create_server(self.service, allow_agent_submission=True)
        ) as client:
            status = await self.call(client, "server_status")
            self.assertTrue(status["can_submit"])
            self.assertFalse(status["client_form_elicitation"])
            self.assertEqual(status["submission_tool"], "submit_review")
            book = await self.review()
            source = await self.finish(self.service.start("bookings"))
            cancel = self.service.review_cancellation(
                source.id, ROW["reference_number"], "Plans changed"
            )
            source = await self.finish(self.service.start("tasks"))
            approve = self.service.review_task_approval(
                source.id, TASK["reference_number"]
            )
            for review in (book, cancel, approve):
                job = await self.call(
                    client, "submit_review", review_id=review["review_id"]
                )
                await self.finish(job)
                replay = await self.call(
                    client, "submit_review", review_id=review["review_id"]
                )
                self.assertEqual(replay["operation_id"], job["operation_id"])
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(
                set(tools["submit_review"].input_schema["properties"]), {"review_id"}
            )
            self.assertTrue(tools["submit_review"].annotations.idempotent_hint)
        for kind in ("book", "cancel", "approve_task"):
            self.assertEqual([name for name, _ in self.backend.calls].count(kind), 1)

    async def test_human_mode_does_not_expose_agent_submission(self):
        async with Client(create_server(self.service)) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            self.assertNotIn("submit_review", tools)
            status = await self.call(client, "server_status")
            self.assertFalse(status["can_submit"])
            result = await client.call_tool("submit_review", {"review_id": "anything"})
            self.assertTrue(result.is_error)
        self.assertEqual(self.backend.calls, [])

    async def test_iso_search_partial_overrides_and_wait_over_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = PersonalStore(Path(directory) / "preferences.sqlite3", "local")
            profile.update_preferences({"floors": ["Level 2"], "duration_hrs": 3})
            self.service.profile = profile
            async with Client(create_server(self.service)) as client:
                operation = await self.call(
                    client,
                    "search_rooms",
                    preferences={
                        "date_raw": "2099-11-01",
                        "start_time": "14:00",
                        "duration_hrs": 2,
                        "floors": None,
                    },
                )
                result = await self.call(
                    client, "wait_operation", operation_id=operation["operation_id"]
                )
                self.assertEqual(result["status"], "succeeded")
                config = self.backend.calls[-1][1]
                self.assertEqual(config.date, "01-Nov-2099")
                self.assertEqual(config.end_time, "16:00")
                self.assertEqual(config.floors, ("Level 2", "Level 3", "Level 4"))
                self.assertEqual(
                    profile.preferences(), {"floors": ["Level 2"], "duration_hrs": 3}
                )
                available = await self.call(
                    client, "available_rooms", operation_id=operation["operation_id"]
                )
                self.assertEqual(available["available_total"], 1)
                operation = await self.call(
                    client, "search_rooms", config={**CONFIG, "date": "2099-11-01"}
                )
                await self.finish(operation)
                invalid = await client.call_tool(
                    "search_rooms", {"config": CONFIG, "preferences": {}}
                )
                self.assertTrue(invalid.is_error)

    async def test_workflow_resource_and_prompt_match_host_policy(self):
        async with Client(
            create_server(self.service, allow_agent_submission=True)
        ) as client:
            resources = await client.list_resources()
            self.assertIn(
                "sagasu://workflow",
                [str(resource.uri) for resource in resources.resources],
            )
            resource = await client.read_resource("sagasu://workflow")
            workflow = json.loads(resource.contents[0].text)
            self.assertEqual(workflow["submission_tool"], "submit_review")
            prompt = await client.get_prompt(
                "book_room", {"request": "Book a room tomorrow"}
            )
            self.assertIn("Book a room tomorrow", prompt.messages[0].content.text)


class HostSettingsTests(unittest.TestCase):
    def test_invalid_confirmation_policy_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {}, clear=True),
        ):
            env = Path(directory) / ".env"
            env.write_text("SAGASU_MCP_CONFIRMATION=unknown\n")
            with self.assertRaises(ConfigError):
                Settings.load(env).validate()
