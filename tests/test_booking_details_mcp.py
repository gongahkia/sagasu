"""Booking metadata follows the normal MCP queue without submitting FBS actions."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mcp import Client

from sagasu.action_journal import ActionJournal
from sagasu.booking_details import BookingDetailsUnavailable, parse_booking_details
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentError, AgentService, Backend
from tests.test_booking_details import RECORD, snapshot
from tests.test_mcp import FakeBackend


class DetailBackend(FakeBackend):
    async def run(self, kind, payload, progress, notify):
        if kind == "booking_details":
            self.calls.append((kind, payload))
            if self.error:
                raise self.error
            await progress("reading the verified booking details")
            return parse_booking_details(snapshot(), RECORD, include_additional=payload["include_additional"])
        return await super().run(kind, payload, progress, notify)


class BookingDetailProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = DetailBackend()
        self.service = AgentService(backend=self.backend)
        self.addAsyncCleanup(self.service.close)

    async def call(self, client, name, arguments=None):
        response = await client.call_tool(name, arguments or {})
        self.assertFalse(response.is_error, response.content)
        return response.structured_content

    async def test_discovery_readonly_annotations_and_complete_metadata_roundtrip(self):
        async with Client(create_server(self.service, allow_agent_submission=True)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            tool = tools["booking_details"]
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertTrue(tool.annotations.open_world_hint)
            self.assertEqual(tool.input_schema["properties"]["include_additional"]["default"], True)
            self.assertNotIn("include_comments", tool.input_schema["properties"])
            started = await self.call(client, "booking_details", {"reference_number": RECORD["reference_number"], "source": "bookings"})
            finished = await self.call(client, "wait_operation", {"operation_id": started["operation_id"]})
            self.assertEqual(finished["status"], "succeeded")
            result = finished["result"]
            self.assertTrue(result["reference_verified"])
            self.assertEqual(result["booked_by"]["name"], "Original Booker")
            self.assertEqual(result["additional_details"]["credits_deducted"], "12.00")
            self.assertEqual(result["field_status"]["confirmed_by"], "not_exposed")
            self.assertFalse(result["workflow_comments_included"])
            self.assertEqual([kind for kind, _ in self.backend.calls], ["booking_details"])
            self.assertEqual(self.service.reviews, {})

    async def test_summary_points_to_details_and_additional_fields_can_be_omitted(self):
        async with Client(create_server(self.service)) as client:
            started = await self.call(client, "list_bookings")
            result = await self.call(client, "wait_operation", {"operation_id": started["operation_id"]})
            self.assertEqual(result["result"]["details_tool"], "booking_details")
            started = await self.call(client, "booking_details", {"reference_number": RECORD["reference_number"], "include_additional": False})
            result = await self.call(client, "wait_operation", {"operation_id": started["operation_id"]})
            self.assertNotIn("additional_details", result["result"])
            workflow = json.loads((await client.read_resource("sagasu://workflow")).contents[0].text)
            self.assertIn("booking_details", workflow["read_only"])

    async def test_missing_view_has_actionable_failure_without_private_browser_text(self):
        self.backend.error = BookingDetailsUnavailable("PRIVATE_BROWSER_TEXT")
        async with Client(create_server(self.service)) as client:
            started = await self.call(client, "booking_details", {"reference_number": RECORD["reference_number"]})
            result = await self.call(client, "wait_operation", {"operation_id": started["operation_id"]})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["reason"], "booking_details_unavailable")
            self.assertEqual(result["next_tool"], "list_bookings")
            self.assertNotIn("PRIVATE_BROWSER_TEXT", json.dumps(result))

    async def test_invalid_arguments_never_reach_backend(self):
        for reference in ["", "   ", "BK\nTEST", "x"*101, None]:
            with self.assertRaises(AgentError):
                self.service.booking_details(reference)
        with self.assertRaises(AgentError):
            self.service.booking_details("BK-TEST", source="arbitrary")
        with self.assertRaises(AgentError):
            self.service.booking_details("BK-TEST", include_additional="true")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.service.jobs, {})

    async def test_read_metadata_is_not_persisted_in_durable_action_history(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ActionJournal(Path(directory)/"actions.sqlite3")
            service = AgentService(backend=self.backend, journal=journal)
            self.addAsyncCleanup(service.close)
            journal.history()
            job = service.booking_details(RECORD["reference_number"])
            await service.get(job["operation_id"]).task
            self.assertEqual(journal.history()["total"], 0)
            for value in [b"Original Booker", b"Co Booker", b"research meeting"]:
                self.assertNotIn(value, journal.path.read_bytes())


class BookingDetailBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_route_uses_only_readonly_browser_reader_even_in_api_mode(self):
        browser = Mock()
        browser.__aenter__ = AsyncMock(return_value=browser)
        browser.__aexit__ = AsyncMock(return_value=False)
        read = AsyncMock(return_value={"reference_number": "BK-TEST", "reference_verified": True})
        with patch.dict(os.environ, {"SAGASU_READ_BACKEND": "api"}), patch("sagasu.mcp_service.FBSClient", return_value=browser), patch("sagasu.booking_details.read_booking_details", read), patch("sagasu.mcp_service.execute_booking", AsyncMock()) as booking, patch("sagasu.mcp_service.execute_task_action", AsyncMock()) as task:
            backend = Backend()
            result = await backend.run("booking_details", {"reference_number": "BK-TEST", "source": "tasks", "include_additional": True}, AsyncMock(), AsyncMock())
        self.assertTrue(result["reference_verified"])
        read.assert_awaited_once_with(browser, "BK-TEST", source="tasks", include_additional=True)
        booking.assert_not_awaited()
        task.assert_not_awaited()
        self.assertIn("booking_details", backend.capabilities()["browser_workflows"])
