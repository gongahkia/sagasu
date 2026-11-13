"""Broken setup remains diagnosable over actual MCP connections; never uses FBS."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client, StdioServerParameters

from sagasu.config import Settings
from sagasu.host import control
from sagasu.mcp_runtime import MCPRuntime
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService
from sagasu.runtime import AlreadyRunning, InstanceLock
from tests.test_mcp import CONFIG, FakeBackend


class DiagnosticMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = self.root / ".env"
        self.base = "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\nSAGASU_JOURNAL_PATH=actions.sqlite3\n"
        self.env.write_text(self.base)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    async def call(self, client, name, args=None):
        response = await client.call_tool(name, args or {})
        self.assertFalse(response.is_error, response.content)
        return response.structured_content

    async def test_diagnostic_tools_and_structured_failures_remain_available(self):
        self.env.write_text(
            self.base
            + "SAGASU_AUTH_TIMEOUT_MS=PRIVATE_INVALID_VALUE\nSMU_LOGIN_PASSWORD=PRIVATE_PASSWORD\n"
        )
        settings = Settings.load(self.env, diagnostic=True)
        with (
            MCPRuntime(settings) as runtime,
            patch("sagasu.mcp_service.FBSClient") as browser,
        ):
            self.addAsyncCleanup(runtime.service.close)
            server = create_server(
                runtime.service, configuration_provider=runtime.status
            )
            async with Client(server) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                self.assertTrue(
                    {
                        "server_status",
                        "configuration_status",
                        "search_rooms",
                        "list_bookings",
                        "login",
                    }
                    <= tools.keys()
                )
                self.assertTrue(
                    tools["configuration_status"].annotations.read_only_hint
                )
                self.assertFalse(
                    tools["configuration_status"].annotations.open_world_hint
                )
                status = await self.call(client, "server_status")
                self.assertEqual(status["mode"], "diagnostic")
                self.assertFalse(status["can_submit"])
                setup = await self.call(client, "configuration_status")
                self.assertFalse(setup["operational"])
                self.assertEqual(status["configuration"], setup)
                for name, arguments in [
                    ("list_bookings", {}),
                    ("login", {}),
                    ("session_status", {}),
                    ("search_rooms", {"config": CONFIG}),
                ]:
                    operation = await self.call(client, name, arguments)
                    result = await self.call(
                        client,
                        "wait_operation",
                        {"operation_id": operation["operation_id"]},
                    )
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(
                        result["error"]["reason"], "configuration_required"
                    )
                    self.assertEqual(
                        result["error"]["next_tool"], "configuration_status"
                    )
                    self.assertNotIn("result", result)
                personal = await client.call_tool("list_cobookers", {})
                self.assertTrue(personal.is_error)
                self.assertIn("configuration_required", str(personal.content))
                history = await client.call_tool("action_history", {})
                self.assertTrue(history.is_error)
                self.assertIn("configuration_required", str(history.content))
                workflow = json.loads(
                    (await client.read_resource("sagasu://workflow")).contents[0].text
                )
                self.assertIn("configuration_status", json.dumps(workflow["setup"]))
                exposed = json.dumps(
                    {
                        "status": status,
                        "setup": setup,
                        "result": result,
                        "workflow": workflow,
                    }
                )
                self.assertNotIn("PRIVATE_INVALID_VALUE", exposed)
                self.assertNotIn("PRIVATE_PASSWORD", exposed)
            browser.assert_not_called()
        self.assertFalse(settings.profile.exists())
        self.assertFalse(settings.preferences.exists())
        self.assertFalse(settings.journal.exists())

    async def test_missing_headless_credentials_preserve_working_reads(self):
        self.env.write_text(self.base + "SAGASU_HEADLESS=true\n")
        settings = Settings.load(self.env)
        backend = FakeBackend()
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        with MCPRuntime(settings, service=service) as runtime:
            async with Client(
                create_server(service, configuration_provider=runtime.status)
            ) as client:
                setup = await self.call(client, "configuration_status")
                self.assertTrue(setup["operational"])
                self.assertFalse(setup["capabilities"]["automatic_login"])
                operation = await self.call(client, "list_bookings")
                result = await self.call(
                    client,
                    "wait_operation",
                    {"operation_id": operation["operation_id"]},
                )
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(len(result["result"]["bookings"]), 1)
                self.assertEqual([kind for kind, _ in backend.calls], ["bookings"])

    async def test_unreadable_private_databases_are_preserved_and_diagnosed(self):
        for filename in ["actions.sqlite3", "personal.sqlite3"]:
            with self.subTest(filename=filename):
                database = self.root / filename
                original = b"PRIVATE_DATABASE_SENTINEL"
                database.write_bytes(original)
                settings = Settings.load(self.env)
                with MCPRuntime(settings) as runtime:
                    self.addAsyncCleanup(runtime.service.close)
                    report = runtime.status()
                    self.assertEqual(report["mode"], "diagnostic")
                    self.assertIn(
                        "runtime.storage", {check["name"] for check in report["checks"]}
                    )
                    self.assertNotIn("PRIVATE_DATABASE_SENTINEL", json.dumps(report))
                    with InstanceLock(settings.profile):
                        pass
                self.assertEqual(database.read_bytes(), original)
                database.unlink()


@unittest.skipIf(os.name == "nt", "Shared Unix host")
class DiagnosticStdioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = self.root / ".env"
        self.base = "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\nSAGASU_JOURNAL_PATH=actions.sqlite3\n"
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("SAGASU_", "SMU_"))
        }

    def settings(self):
        with patch.dict(os.environ, self.environment, clear=True):
            return Settings.load(self.env, diagnostic=True)

    def parameters(self, direct=False):
        return StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "sagasu",
                "mcp",
                "--env-file",
                str(self.env),
                *(["--direct"] if direct else []),
            ],
            env=self.environment,
        )

    async def stop_host(self):
        settings = self.settings()
        await control(settings, "stop")
        for _ in range(100):
            if not (await control(settings, "status")).get("running"):
                break
            await asyncio.sleep(0.02)

    async def test_bad_config_is_diagnosable_then_reconnect_recovers_operations(self):
        self.env.write_text(
            self.base
            + "SAGASU_AUTH_TIMEOUT_MS=PRIVATE_INVALID_VALUE\nSAGASU_READ_BACKEND=invalid\nSMU_LOGIN_PASSWORD=PRIVATE_PASSWORD\n"
        )
        self.addAsyncCleanup(self.stop_host)
        async with Client(self.parameters()) as client:
            response = await client.call_tool("configuration_status", {})
            self.assertFalse(response.is_error)
            setup = response.structured_content
            self.assertFalse(setup["operational"])
            self.assertEqual(setup["mode"], "diagnostic")
            self.assertNotIn("PRIVATE_INVALID_VALUE", json.dumps(setup))
            self.assertNotIn("PRIVATE_PASSWORD", json.dumps(setup))
            operation = (await client.call_tool("list_bookings", {})).structured_content
            failed = (
                await client.call_tool(
                    "wait_operation", {"operation_id": operation["operation_id"]}
                )
            ).structured_content
            self.assertEqual(failed["error"]["reason"], "configuration_required")
        self.env.write_text(self.base)
        await self.stop_host()
        async with Client(self.parameters()) as client:
            status = (await client.call_tool("server_status", {})).structured_content
            self.assertTrue(status["operational"])
            self.assertTrue(status["durable_action_history"])
            self.assertFalse(
                (await client.call_tool("get_search_preferences", {})).is_error
            )

    async def test_direct_mode_starts_diagnostics_for_invalid_config(self):
        self.env.write_text(self.base + "SAGASU_BROWSER_CHANNEL=invalid\n")
        async with Client(self.parameters(direct=True)) as client:
            response = await client.call_tool("server_status", {})
            self.assertFalse(response.is_error)
            self.assertEqual(response.structured_content["mode"], "diagnostic")
        self.assertFalse((self.root / "browser").exists())

    async def test_owned_profile_returns_diagnostics_without_releasing_owner_lock(self):
        self.env.write_text(self.base)
        settings = self.settings()
        with InstanceLock(settings.profile):
            async with Client(self.parameters()) as client:
                response = await client.call_tool("configuration_status", {})
                self.assertFalse(response.is_error)
                self.assertEqual(response.structured_content["mode"], "diagnostic")
                self.assertIn(
                    "runtime.host",
                    {check["name"] for check in response.structured_content["checks"]},
                )
                with self.assertRaises(AlreadyRunning):
                    with InstanceLock(settings.profile):
                        pass


if __name__ == "__main__":
    unittest.main()
