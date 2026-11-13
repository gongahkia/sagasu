"""Independent MCP clients share a browser owner and serialized durable jobs."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sagasu.config import Settings, ConfigError
from sagasu.host import serve_host, client_session, socket_path, control, run_operation
from sagasu.mcp_service import AgentService
from tests.test_mcp import FakeBackend


@unittest.skipIf(os.name == "nt", "Unix host")
class SharedHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = Path(self.directory.name) / ".env"
        env.write_text(
            "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(env)
        self.backend = FakeBackend()
        self.service = AgentService(backend=self.backend)
        self.host = asyncio.create_task(serve_host(self.settings, service=self.service))
        self.addAsyncCleanup(self.stop_host)
        for _ in range(100):
            if socket_path(self.settings).exists():
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.host.done())

    async def stop_host(self):
        self.backend.gate = None
        if not self.host.done():
            self.host.cancel()
        await asyncio.gather(self.host, return_exceptions=True)

    async def call(self, session, name, arguments=None):
        result = await session.call_tool(name, arguments or {})
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def test_clients_share_operations_and_disconnect_does_not_cancel(self):
        self.backend.gate = asyncio.Event()
        async with client_session(self.settings) as first:
            job = await self.call(first, "login")
            await asyncio.sleep(0.01)
        async with client_session(self.settings) as second:
            status = await self.call(
                second, "operation_status", {"operation_id": job["operation_id"]}
            )
            self.assertFalse(status["terminal"])
            refusal = await control(self.settings, "stop")
            self.assertTrue(refusal["busy"])
            self.assertFalse(refusal["stopping"])
            self.backend.gate.set()
            status = await self.call(
                second, "wait_operation", {"operation_id": job["operation_id"]}
            )
            self.assertEqual(status["status"], "succeeded")
            self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(socket_path(self.settings).stat().st_mode & 0o777, 0o600)

    async def test_two_clients_have_independent_sessions_and_one_queue(self):
        async with (
            client_session(self.settings) as first,
            client_session(self.settings) as second,
        ):
            a = await self.call(first, "login")
            b = await self.call(second, "login")
            self.assertNotEqual(a["operation_id"], b["operation_id"])
            for session, job in [(first, a), (second, b)]:
                result = await self.call(
                    session, "wait_operation", {"operation_id": job["operation_id"]}
                )
                self.assertEqual(result["status"], "succeeded")
            self.assertEqual(
                (await self.call(first, "server_status"))["host_pid"], os.getpid()
            )
            self.assertEqual(len(self.backend.calls), 2)

    async def test_cli_operation_uses_existing_host(self):
        result = await run_operation(self.settings, "login")
        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(len(self.backend.calls), 1)

    async def test_cli_failure_keeps_safe_recovery_message(self):
        from sagasu.fbs_client import AuthenticationRequired

        self.backend.error = AuthenticationRequired(
            "PRIVATE", reason="missing_password"
        )
        with self.assertRaisesRegex(ConfigError, "SMU_LOGIN_PASSWORD"):
            await run_operation(self.settings, "login")

    async def test_second_client_waits_for_first_browser_operation(self):
        self.backend.gate = asyncio.Event()
        async with (
            client_session(self.settings) as first,
            client_session(self.settings) as second,
        ):
            a = await self.call(first, "login")
            b = await self.call(second, "login")
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.backend.calls), 1)
            queued = await self.call(
                second, "operation_status", {"operation_id": b["operation_id"]}
            )
            self.assertEqual(queued["status"], "queued")
            self.backend.gate.set()
            for job in (a, b):
                await self.call(
                    first, "wait_operation", {"operation_id": job["operation_id"]}
                )
            self.assertEqual(len(self.backend.calls), 2)

    async def test_configuration_change_requires_explicit_host_restart(self):
        from dataclasses import replace

        changed = replace(self.settings, values={"SAGASU_HEADLESS": "true"})
        with self.assertRaises(ConfigError):
            async with client_session(changed):
                pass
        self.assertTrue((await control(changed, "status"))["running"])
        self.assertTrue((await control(changed, "stop"))["stopping"])
        await asyncio.wait_for(self.host, 5)

    async def test_stop_releases_profile_while_an_idle_mcp_client_remains_connected(
        self,
    ):
        from sagasu.runtime import InstanceLock

        async with client_session(self.settings) as client:
            await self.call(client, "server_status")
            result = await control(self.settings, "stop")
            self.assertTrue(result["stopping"])
            self.assertTrue(result["shutdown_complete"])
            await asyncio.wait_for(asyncio.shield(self.host), 3)
            with InstanceLock(self.settings.profile):
                pass


@unittest.skipIf(os.name == "nt", "Unix host")
class HostLauncherTests(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_stdio_clients_launch_only_one_host(self):
        import sys
        from mcp import Client, StdioServerParameters

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\nSAGASU_JOURNAL_PATH=actions.sqlite3\n"
            )
            environment = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("SAGASU_", "SMU_"))
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.load(env_file)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "sagasu", "mcp", "--env-file", str(env_file)],
                env=environment,
            )
            ready = asyncio.Event()
            pids = []
            statuses = []

            async def connect_one():
                async with Client(params) as client:
                    result = await client.call_tool("server_status", {})
                    self.assertFalse(result.is_error)
                    statuses.append(result.structured_content)
                    pids.append(result.structured_content["host_pid"])
                    if len(pids) == 2:
                        ready.set()
                    await ready.wait()
                    resource = await client.read_resource("sagasu://workflow")
                    workflow = json.loads(resource.contents[0].text)
                    self.assertIn("repeat_booking", workflow)
                    self.assertIn("alternatives", workflow)

            try:
                await asyncio.wait_for(asyncio.gather(connect_one(), connect_one()), 30)
                self.assertEqual(len(set(pids)), 1, statuses)
                self.assertNotEqual(pids[0], os.getpid())
                status = await control(settings, "status")
                self.assertEqual(status["pid"], pids[0])
                self.assertFalse(status["busy"])
            finally:
                await control(settings, "stop")


@unittest.skipIf(os.name == "nt", "Unix host")
class HostHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_shutdown_handshake_is_retried_without_parsing_empty_json(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            env = Path(directory) / ".env"
            env.write_text("SAGASU_BROWSER_PROFILE=browser\n")
            settings = Settings.load(env)
            reader = Mock(readline=AsyncMock(return_value=b""))
            writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
            with patch(
                "sagasu.host.asyncio.open_unix_connection",
                AsyncMock(side_effect=[(reader, writer), FileNotFoundError()]),
            ) as connect:
                result = await control(settings, "status")
            self.assertEqual(result, {"running": False})
            self.assertEqual(connect.await_count, 2)
            writer.close.assert_called_once()
