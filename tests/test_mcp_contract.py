"""Reject ignored arguments before any operation, review or resolver can run."""

import json
import unittest

from mcp import Client

from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService
from tests.test_mcp import FakeBackend


class MCPInputContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend()
        self.service = AgentService(backend=self.backend)
        self.addAsyncCleanup(self.service.close)

    async def test_unknown_arguments_never_start_an_operation_or_echo_values(self):
        async with Client(create_server(self.service)) as client:
            tools = (await client.list_tools()).tools
            self.assertTrue(
                all(
                    tool.input_schema.get("additionalProperties") is False
                    for tool in tools
                )
            )
            failed = await client.call_tool(
                "list_bookings", {"limit": 1, "offset": 0, "typo": "PRIVATE_VALUE"}
            )
            self.assertTrue(failed.is_error)
            text = json.dumps(
                [item.text for item in failed.content if item.type == "text"]
            )
            self.assertIn("invalid_tool_arguments", text)
            self.assertIn("takes no arguments", text)
            self.assertNotIn("PRIVATE_VALUE", text)
            self.assertEqual(self.service.jobs, {})
            self.assertEqual(self.backend.calls, [])
            started = (await client.call_tool("list_bookings", {})).structured_content
            finished = (
                await client.call_tool(
                    "wait_operation",
                    {"operation_id": started["operation_id"], "limit": 1},
                )
            ).structured_content
            self.assertEqual(finished["status"], "succeeded")
            page = finished["result"]["pagination"]
            self.assertEqual(
                page["returned_count"], len(finished["result"]["bookings"])
            )
            self.assertEqual(page["retained_total"], page["returned_total"])

    async def test_page_bounds_are_in_schema_and_bad_poll_does_not_restart_job(self):
        async with Client(create_server(self.service)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in (
                "wait_operation",
                "operation_result",
                "available_rooms",
                "list_cobookers",
                "action_history",
            ):
                schema = tools[name].input_schema["properties"]
                self.assertEqual(schema["limit"]["maximum"], 50)
                self.assertEqual(schema["limit"]["minimum"], 1)
                self.assertEqual(schema["offset"]["minimum"], 0)
            started = (await client.call_tool("list_bookings", {})).structured_content
            failed = await client.call_tool(
                "wait_operation",
                {"operation_id": started["operation_id"], "limit": 100},
            )
            self.assertTrue(failed.is_error)
            finished = (
                await client.call_tool(
                    "wait_operation",
                    {"operation_id": started["operation_id"], "limit": 50},
                )
            ).structured_content
            self.assertEqual(finished["status"], "succeeded")
            self.assertEqual(len(self.service.jobs), 1)
            self.assertEqual(len(self.backend.calls), 1)

    async def test_forged_confirmation_fields_are_rejected_before_submission(self):
        async with Client(
            create_server(self.service, allow_agent_submission=True)
        ) as client:
            failed = await client.call_tool(
                "submit_review", {"review_id": "not-a-review", "approved": True}
            )
            self.assertTrue(failed.is_error)
            self.assertIn(
                "invalid_tool_arguments",
                json.dumps(
                    [item.text for item in failed.content if item.type == "text"]
                ),
            )
        self.assertEqual(self.service.jobs, {})
        self.assertEqual(self.backend.calls, [])
