import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sagasu.backend_probe import probe_client, run_probe
from sagasu.cli import main
from sagasu.config import ConfigError, Settings
from sagasu.fbs_client import (
    AuthenticationRequired,
    BookingOutcomeUnknown,
    SearchConfig,
)
from sagasu.mcp_service import Backend
from sagasu.read_backends import HybridReads
from sagasu.runtime import InstanceLock


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(
            ensure_authenticated=AsyncMock(),
            search=AsyncMock(
                return_value=SimpleNamespace(rooms=["fixture"], bookable_rooms=[])
            ),
        )
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=False)
        self.reads = HybridReads("auto")
        self.reads.verify_account = AsyncMock(
            side_effect=lambda c, k: {
                k: [
                    {
                        "reference_number": "PRIVATE_ACCOUNT_ROW",
                        "requested_by": "PRIVATE_NAME",
                    }
                ],
                "total": 1,
                "complete": True,
                "read_backend": {
                    "name": "browser",
                    "api_verification": "unverified",
                    "fallback_reason": "no_observed_get_document",
                },
            }
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = Path(directory.name) / ".env"
        env.write_text(
            "SAGASU_BROWSER_PROFILE=browser\nSAGASU_PREFERENCES_PATH=personal.sqlite3\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(env)

    async def test_report_has_shapes_and_counts_without_account_rows(self):
        report = await probe_client(self.client, self.reads, {})
        self.assertTrue(report["ok"])
        self.assertTrue(report["read_only"])
        self.assertEqual(report["recommendation"], "browser")
        self.assertEqual(
            [c["name"] for c in report["checks"]],
            ["session", "search", "bookings", "tasks"],
        )
        self.assertNotIn("PRIVATE_", json.dumps(report))
        self.client.book.assert_not_called()

    async def test_expired_session_never_searches_or_attempts_credentials(self):
        self.client.ensure_authenticated.side_effect = AuthenticationRequired(
            "PRIVATE_SESSION"
        )
        report = await probe_client(self.client, self.reads, {})
        self.assertFalse(report["ok"])
        self.assertNotIn("PRIVATE_SESSION", json.dumps(report))
        self.assertEqual(report["checks"][0]["error_code"], "AuthenticationRequired")
        self.client.search.assert_not_awaited()
        self.reads.verify_account.assert_not_awaited()
        self.client._complete_interactive_authentication.assert_not_called()

    async def test_schema_failure_is_reported_and_other_reads_continue(self):
        self.client.search.side_effect = RuntimeError("PRIVATE_RESPONSE_BODY")
        report = await probe_client(self.client, self.reads, {})
        self.assertFalse(report["ok"])
        self.assertEqual(self.reads.verify_account.await_count, 2)
        self.assertNotIn("PRIVATE_RESPONSE_BODY", json.dumps(report))

    async def test_direct_probe_uses_noninteractive_profile_and_closes_it(self):
        with (
            patch("sagasu.host.control", AsyncMock(return_value={"running": False})),
            patch(
                "sagasu.backend_probe.FBSClient", return_value=self.client
            ) as constructor,
            patch("sagasu.backend_probe.HybridReads", return_value=self.reads),
        ):
            report = await run_probe(self.settings)
        self.assertTrue(report["ok"])
        constructor.assert_called_once_with(
            interactive=False, keep_alive=False, profile_path=self.settings.profile
        )
        self.client.__aexit__.assert_awaited_once()
        with InstanceLock(self.settings.profile):
            pass

    async def test_timeout_closes_client_and_releases_profile(self):
        async def wait_forever():
            await asyncio.Event().wait()

        self.client.ensure_authenticated.side_effect = wait_forever
        with (
            patch("sagasu.host.control", AsyncMock(return_value={"running": False})),
            patch("sagasu.backend_probe.FBSClient", return_value=self.client),
        ):
            report = await run_probe(self.settings, timeout=0.01)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["error_code"], "TimeoutError")
        self.client.__aexit__.assert_awaited_once()
        with InstanceLock(self.settings.profile):
            pass

    async def test_running_host_uses_queue_without_second_browser(self):
        report = {"ok": True, "read_only": True, "checks": []}
        with (
            patch("sagasu.host.control", AsyncMock(return_value={"running": True})),
            patch("sagasu.host.run_operation", AsyncMock(return_value=report)) as run,
            patch("sagasu.backend_probe.FBSClient") as browser,
        ):
            self.assertEqual(await run_probe(self.settings), report)
        run.assert_awaited_once_with(self.settings, "probe_read_backends")
        browser.assert_not_called()


class ProbeConfigTests(unittest.TestCase):
    def test_cli_requires_explicit_live_before_configuration_or_browser(self):
        for args in [["backend-probe"], ["backend-probe", "--live", "--timeout", "0"]]:
            with (
                patch("sagasu.backend_probe.FBSClient") as browser,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                main(args)
            self.assertEqual(caught.exception.code, 2)
            browser.assert_not_called()

    def test_config_validates_and_exposes_mode_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            for mode in ["auto", "browser", "api", "invalid"]:
                env.write_text(
                    f"SAGASU_READ_BACKEND={mode}\nSMU_LOGIN_EMAIL=PRIVATE_EMAIL\nSMU_LOGIN_PASSWORD=PRIVATE_PASSWORD\n"
                )
                with patch.dict(os.environ, {}, clear=True):
                    settings = Settings.load(env)
                if mode == "invalid":
                    with self.assertRaises(ConfigError):
                        settings.validate()
                else:
                    settings.validate()
                    self.assertEqual(settings.public()["read_backend"], mode)
                    self.assertNotIn("PRIVATE_", json.dumps(settings.public()))


class BackendRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_tool_is_read_only_and_uses_existing_job_workflow(self):
        from mcp.client import Client
        from sagasu.mcp_server import create_server
        from sagasu.mcp_service import AgentService

        backend = Mock(run=AsyncMock(return_value={"ok": True, "read_only": True}))
        service = AgentService(backend=backend)
        async with Client(create_server(service)) as client:
            tools = (await client.list_tools()).tools
            probe = next(t for t in tools if t.name == "probe_read_backends")
            self.assertTrue(probe.annotations.read_only_hint)
            result = await client.call_tool("probe_read_backends", {})
            self.assertFalse(result.is_error, result.content)
            operation = result.structured_content["operation_id"]
            result = await client.call_tool(
                "wait_operation", {"operation_id": operation}
            )
            self.assertEqual(result.structured_content["status"], "succeeded")
            self.assertTrue(result.structured_content["result"]["read_only"])
        self.assertEqual(backend.run.await_args.args[0], "probe")

    async def test_book_unknown_outcome_does_not_invoke_any_read_backend(self):
        backend = Backend()
        backend.reads.account = AsyncMock()
        backend.reads.search = AsyncMock()
        with patch(
            "sagasu.mcp_service.execute_booking",
            AsyncMock(side_effect=BookingOutcomeUnknown("unknown")),
        ) as execute:
            with self.assertRaises(BookingOutcomeUnknown):
                await backend.run("book", "request", None, None)
        execute.assert_awaited_once()
        backend.reads.account.assert_not_awaited()
        backend.reads.search.assert_not_awaited()

    async def test_cancellation_unknown_does_not_replay_or_invoke_http(self):
        from sagasu.fbs_account import CancellationOutcomeUnknown

        backend = Backend()
        backend.reads.account = AsyncMock()
        client = Mock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("sagasu.mcp_service.FBSClient", return_value=client),
            patch(
                "sagasu.mcp_service.cancel_booking",
                AsyncMock(side_effect=CancellationOutcomeUnknown("unknown")),
            ) as cancel,
        ):
            with self.assertRaises(CancellationOutcomeUnknown):
                await backend.run(
                    "cancel",
                    ({"reference_number": "BK-FIXTURE"}, "requested"),
                    None,
                    None,
                )
        cancel.assert_awaited_once()
        backend.reads.account.assert_not_awaited()

    async def test_domain_account_reads_use_router_and_probe_stays_noninteractive(self):
        backend = Backend()
        backend.reads.account = AsyncMock(return_value={"bookings": [], "total": 0})
        client = Mock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("sagasu.mcp_service.FBSClient", return_value=client) as constructor:
            result = await backend.run("bookings", None, None, None)
            self.assertEqual(result["total"], 0)
            backend.reads.account.assert_awaited_once_with(client, "bookings")
            with patch(
                "sagasu.backend_probe.probe_client",
                AsyncMock(return_value={"ok": True}),
            ):
                await backend.run("probe", {}, None, None)
            self.assertFalse(constructor.call_args.kwargs["interactive"])

    async def test_search_preserves_horizon_and_declares_browser_backend(self):
        from sagasu.fbs_client import SearchResult, RoomAvailability, TimeSlot

        config = SearchConfig("23-Nov-2026", "10:00", "11:00")
        original = SearchResult(
            config,
            [
                RoomAvailability(
                    "Room A", [TimeSlot("10:00", "11:00", "outside_booking_horizon")]
                )
            ],
            "fixture",
        )
        client = SimpleNamespace(search=AsyncMock(return_value=original))
        result = await HybridReads("auto").search(client, config)
        self.assertEqual(result["booking_horizon"], original.booking_horizon)
        self.assertEqual(result["read_backend"]["name"], "browser")
