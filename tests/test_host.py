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

    async def test_booking_watch_survives_client_disconnect_and_keeps_host_busy(self):
        from tests.test_mcp import ROW

        sleeping = asyncio.Event()
        resume = asyncio.Event()
        original_run = self.backend.run
        reads = 0

        async def run(kind, payload, progress, notify):
            nonlocal reads
            if kind == "bookings":
                reads += 1
                return {
                    "complete": True,
                    "bookings": [
                        {
                            **ROW,
                            "fbs_status": (
                                "Pending Confirmation"
                                if reads == 1
                                else "Pending Approval"
                            ),
                        }
                    ],
                }
            return await original_run(kind, payload, progress, notify)

        async def sleep(seconds):
            sleeping.set()
            await resume.wait()

        self.backend.run = run
        with patch("sagasu.booking_watch.sleep", side_effect=sleep):
            async with client_session(self.settings) as first:
                job = await self.call(
                    first,
                    "watch_booking",
                    {"reference_number": ROW["reference_number"]},
                )
                await asyncio.wait_for(sleeping.wait(), 2)
            refusal = await control(self.settings, "stop")
            self.assertTrue(refusal["busy"])
            self.assertFalse(refusal["stopping"])
            async with client_session(self.settings) as second:
                resume.set()
                result = await self.call(
                    second,
                    "wait_operation",
                    {"operation_id": job["operation_id"], "timeout": 2},
                )
                self.assertTrue(result["result"]["target_status_observed"])
                self.assertEqual(reads, 2)

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

    async def test_status_explains_mismatch_without_exposing_secrets_or_digests(self):
        from dataclasses import replace
        from sagasu.host import identity_parts

        changed = replace(
            self.settings,
            values={
                "SMU_LOGIN_PASSWORD": "PRIVATE_PASSWORD",
                "SMU_LOGIN_EMAIL": "PRIVATE_EMAIL",
            },
        )
        status = await control(changed, "status")
        self.assertFalse(status["compatible"])
        self.assertEqual(status["mismatch"], ["configuration"])
        self.assertTrue(status["can_recover"])
        serialized = json.dumps(status)
        for private in [
            "PRIVATE_PASSWORD",
            "PRIVATE_EMAIL",
            *identity_parts(changed).values(),
        ]:
            self.assertNotIn(private, serialized)

    async def test_recovery_refuses_replacement_host_and_pending_review(self):
        status = await control(self.settings, "recover", expected_pid=-1)
        self.assertEqual(status["blockers"], ["host_changed"])
        self.assertFalse(status["stopping"])
        self.service._review("book", {}, {"room": "PRIVATE_ROOM"})
        status = await control(self.settings, "recover", expected_pid=os.getpid())
        self.assertIn("pending_reviews", status["blockers"])
        self.assertFalse(status["stopping"])
        self.assertNotIn("PRIVATE_ROOM", json.dumps(status))
        self.assertFalse(self.host.done())

    async def test_recovery_refuses_active_work_even_when_discard_is_requested(self):
        self.backend.gate = asyncio.Event()
        async with client_session(self.settings) as client:
            job = await self.call(client, "login")
            status = await control(
                self.settings,
                "recover",
                expected_pid=os.getpid(),
                discard_completed_results=True,
            )
            self.assertIn("active_operations", status["blockers"])
            self.assertFalse(status["stopping"])
            self.backend.gate.set()
            self.assertEqual(
                (
                    await self.call(
                        client, "wait_operation", {"operation_id": job["operation_id"]}
                    )
                )["status"],
                "succeeded",
            )

    async def test_recovery_preserves_finished_results_unless_explicitly_discarded(
        self,
    ):
        async with client_session(self.settings) as client:
            job = await self.call(client, "login")
            await self.call(
                client, "wait_operation", {"operation_id": job["operation_id"]}
            )
        result = await control(self.settings, "recover", expected_pid=os.getpid())
        self.assertEqual(result["blockers"], ["retained_operations"])
        result = await control(
            self.settings,
            "recover",
            expected_pid=os.getpid(),
            discard_completed_results=True,
        )
        self.assertTrue(result["stopping"])
        self.assertTrue(result["shutdown_complete"])
        self.assertTrue(self.service.closed)
        from sagasu.mcp_service import AgentError

        with self.assertRaises(AgentError):
            self.service.start("login")
        with self.assertRaises(AgentError):
            self.service._review("book", {}, {})

    async def test_automatic_recovery_never_switches_configuration(self):
        from dataclasses import replace

        changed = replace(self.settings, values={"SAGASU_HEADLESS": "true"})
        result = await control(
            changed, "recover", expected_pid=os.getpid(), automatic=True
        )
        self.assertEqual(result["blockers"], ["not_code_only"])
        self.assertFalse(result["stopping"])

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
    async def test_concurrent_clients_upgrade_idle_code_only_host_once(self):
        from sagasu.host import identity_parts

        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\nSAGASU_JOURNAL_PATH=actions.sqlite3\n"
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SAGASU_", "SMU_"))
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.load(env)
                old_parts = {**identity_parts(settings), "code": "previous_code"}
                with patch("sagasu.host.identity_parts", return_value=old_parts):
                    host = asyncio.create_task(
                        serve_host(
                            settings, service=AgentService(backend=FakeBackend())
                        )
                    )
                    for _ in range(300):
                        if socket_path(settings).exists():
                            break
                        await asyncio.sleep(0.01)
                    self.assertFalse(host.done())
                ready = asyncio.Event()
                pids = []

                async def attach():
                    async with client_session(settings) as client:
                        response = await client.call_tool("server_status", {})
                        self.assertTrue(response.structured_content["operational"])
                        pids.append(response.structured_content["host_pid"])
                        if len(pids) == 2:
                            ready.set()
                        await ready.wait()

                try:
                    await asyncio.wait_for(asyncio.gather(attach(), attach()), 60)
                    self.assertEqual(len(set(pids)), 1)
                    self.assertNotEqual(pids[0], os.getpid())
                    self.assertTrue(host.done())
                finally:
                    await control(settings, "stop")
                    host.cancel()
                    await asyncio.gather(host, return_exceptions=True)

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
                await asyncio.wait_for(asyncio.gather(connect_one(), connect_one()), 60)
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


class RecoveryEvidenceTests(unittest.TestCase):
    def test_durable_unresolved_write_blocks_recovery_after_memory_expiry(self):
        from sagasu.action_journal import ActionJournal
        from sagasu.host import recovery_state
        from sagasu.mcp_service import Job

        with tempfile.TemporaryDirectory() as directory:
            journal = ActionJournal(Path(directory) / "actions.sqlite3")
            job = Job("unknown", "book", 1, updated_at=1, status="outcome_unknown")
            journal.reserve(job, "review", {})
            journal.update(job)
            service = AgentService(journal=journal, clock=lambda: 4000)
            service.jobs[job.id] = job
            for _ in range(2):
                state = recovery_state(service)
                self.assertEqual(state["retained_operations"], 0)
                self.assertEqual(state["unresolved_outcomes"], 1)
                self.assertFalse(state["can_recover"])
            journal.record_reconciliation(
                job.id, {"state": "confirmed", "action_outcome_verified": True}, 4001
            )
            self.assertTrue(recovery_state(service)["can_recover"])

    def test_unreadable_journal_blocks_recovery_without_leaking_error_text(self):
        from sagasu.action_journal import JournalError
        from sagasu.host import recovery_state

        journal = Mock()
        journal.unresolved_count.side_effect = JournalError("PRIVATE_DIAGNOSTIC")
        state = recovery_state(AgentService(journal=journal))
        self.assertFalse(state["journal_healthy"])
        self.assertFalse(state["can_recover"])
        self.assertNotIn("PRIVATE_DIAGNOSTIC", json.dumps(state))
