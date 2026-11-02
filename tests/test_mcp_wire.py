"""An independent stdlib-only MCP client: JSON-RPC over real subprocess stdio."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


class WireProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        env_file = root / ".env"
        env_file.write_text("SMU_LOGIN_EMAIL=private.fixture@smu.edu.sg\nSMU_LOGIN_PASSWORD=RAW_PROTOCOL_SECRET\nSAGASU_MCP_CONFIRMATION=agent\n")
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("SAGASU_", "SMU_"))}
        environment.update(SAGASU_BROWSER_PROFILE=str(root / "browser"), SAGASU_PREFERENCES_PATH=str(root / "personal.sqlite3"), SAGASU_JOURNAL_PATH=str(root / "actions.sqlite3"))
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-m", "sagasu", "mcp", "--env-file", str(env_file), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.stderr = asyncio.create_task(self.process.stderr.read())
        self.sequence = 0
        self.responses = []
        self.addAsyncCleanup(self.stop)
        initialized = await self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "stdlib-wire-test", "version": "1"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        await self.notify("notifications/initialized")

    async def notify(self, method, params=None):
        packet = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            packet["params"] = params
        self.process.stdin.write((json.dumps(packet) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params=None):
        self.sequence += 1
        identifier = self.sequence
        packet = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            packet["params"] = params
        self.process.stdin.write((json.dumps(packet) + "\n").encode())
        await self.process.stdin.drain()
        async with asyncio.timeout(30):
            while True:
                line = await self.process.stdout.readline()
                self.assertTrue(line, "Server closed stdout before responding")
                # Any banner, debug line or traceback on stdout fails this test.
                response = json.loads(line)
                self.assertEqual(response["jsonrpc"], "2.0")
                self.responses.append(response)
                if response.get("id") == identifier:
                    return response

    async def call(self, name, arguments=None):
        response = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertNotIn("error", response, response)
        return response["result"]

    async def stop(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=20)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
                self.fail("Server did not shut down after stdin EOF")
        errors = (await self.stderr).decode(errors="replace")
        self.assertEqual(self.process.returncode, 0, errors)
        combined = json.dumps(self.responses) + errors
        self.assertNotIn("RAW_PROTOCOL_SECRET", combined)
        self.assertNotIn("private.fixture@smu.edu.sg", combined)

    async def test_discovery_calls_resources_prompts_and_durable_history(self):
        tools = (await self.request("tools/list"))["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertTrue({"search_rooms", "submit_review", "authentication_status", "action_history"} <= names)
        result = await self.call("server_status")
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["structuredContent"]["transport"], "stdio")
        self.assertTrue(result["structuredContent"]["durable_action_history"])
        options = await self.call("search_options")
        self.assertIn("Level 2", options["structuredContent"]["choices"]["floor"])
        self.assertEqual((await self.call("action_history"))["structuredContent"]["total"], 0)
        resources = (await self.request("resources/list"))["result"]["resources"]
        self.assertIn("sagasu://workflow", {row["uri"] for row in resources})
        resource = await self.request("resources/read", {"uri": "sagasu://workflow"})
        self.assertEqual(json.loads(resource["result"]["contents"][0]["text"])["timezone"], "Asia/Singapore")
        prompts = (await self.request("prompts/list"))["result"]["prompts"]
        self.assertIn("book_room", {row["name"] for row in prompts})
        prompt = await self.request("prompts/get", {"name": "book_room", "arguments": {"request": "Search only; do not book"}})
        self.assertIn("Search only; do not book", json.dumps(prompt))

    async def test_invalid_method_tool_and_arguments_preserve_connection(self):
        unknown = await self.request("unrecognized/method")
        self.assertEqual(unknown["error"]["code"], -32601)
        for name, arguments in (("unrecognized_tool", {}), ("operation_status", {"operation_id": 123}), ("action_history", {"limit": 0})):
            with self.subTest(tool=name):
                response = await self.request("tools/call", {"name": name, "arguments": arguments})
                self.assertTrue("error" in response or response.get("result", {}).get("isError"), response)
        self.assertIn("result", await self.request("ping"))
        self.assertEqual((await self.call("action_history"))["structuredContent"]["total"], 0)
