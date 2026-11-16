"""Local host recovery boundaries: no credentials, browser, login or replay."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sagasu.config import Settings
from sagasu.host import HostError
from sagasu.host_recovery import HostRecovery


class HostRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env = Path(directory.name) / ".env"
        self.env.write_text("SAGASU_BROWSER_PROFILE=browser\n")
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings.load(self.env)
        self.controller = HostRecovery(self.settings, diagnostic_frontend=True)

    async def test_legacy_host_returns_exact_commands_without_attempting_restart(self):
        with patch(
            "sagasu.host_recovery.control",
            AsyncMock(return_value={"running": True, "pid": 42, "busy": False}),
        ) as control:
            status = await self.controller.status()
            result = await self.controller.recover(discard_completed_results=True)
        self.assertEqual(status["reason"], "legacy_host")
        self.assertFalse(status["can_recover"])
        self.assertEqual(result["reason"], "legacy_host")
        self.assertEqual(
            result["commands"]["stop_idle_host"],
            [
                sys.executable,
                "-m",
                "sagasu",
                "host",
                "stop",
                "--env-file",
                str(self.env.resolve()),
            ],
        )
        self.assertTrue(
            all(call.args[1] == "status" for call in control.await_args_list)
        )

    async def test_unreachable_host_does_not_leak_exception_or_force_stop(self):
        with patch(
            "sagasu.host_recovery.control",
            AsyncMock(
                side_effect=HostError("PRIVATE_DETAIL", "host_handshake_timeout")
            ),
        ) as control:
            status = await self.controller.status()
            result = await self.controller.recover()
        self.assertEqual(result["reason"], "host_unreachable")
        self.assertNotIn("PRIVATE_DETAIL", json.dumps([status, result]))
        self.assertFalse(result["operations_replayed"])
        self.assertTrue(
            all(call.args[1] == "status" for call in control.await_args_list)
        )

    async def test_status_whitelists_fields_and_never_exports_private_digests(self):
        with patch(
            "sagasu.host_recovery.control",
            AsyncMock(
                return_value={
                    "running": True,
                    "pid": 42,
                    "protocol_version": 2,
                    "compatible": False,
                    "mismatch": ["python"],
                    "identity": {"configuration": "PRIVATE_DIGEST"},
                    "credentials": "PRIVATE_PASSWORD",
                    "can_recover": True,
                }
            ),
        ):
            report = await self.controller.status()
        self.assertEqual(report["mismatch"], ["python"])
        self.assertFalse(report["automatic_code_recovery"])
        self.assertNotIn("PRIVATE_", json.dumps(report))

    async def test_guard_is_authoritative_when_work_starts_after_status(self):
        with (
            patch(
                "sagasu.host_recovery.control",
                AsyncMock(
                    side_effect=[
                        {
                            "running": True,
                            "pid": 42,
                            "protocol_version": 2,
                            "compatible": False,
                            "can_recover": True,
                        },
                        {
                            "running": True,
                            "stopping": False,
                            "blockers": ["active_operations"],
                        },
                    ]
                ),
            ) as control,
            patch("sagasu.host_recovery.connect", AsyncMock()) as launch,
        ):
            result = await self.controller.recover()
        self.assertEqual(result["reason"], "recovery_blocked")
        self.assertEqual(result["blockers"], ["active_operations"])
        self.assertEqual(control.await_args_list[1].kwargs["expected_pid"], 42)
        launch.assert_not_awaited()

    async def test_owner_cannot_restart_itself_mid_response(self):
        controller = HostRecovery(self.settings, owner_pid=42)
        with patch(
            "sagasu.host_recovery.control",
            AsyncMock(
                return_value={
                    "running": True,
                    "pid": 42,
                    "protocol_version": 2,
                    "compatible": False,
                    "mismatch": ["code"],
                }
            ),
        ) as control:
            result = await controller.recover()
        self.assertEqual(result["reason"], "client_reconnect_required")
        self.assertTrue(result["reconnect_required"])
        self.assertEqual(control.await_count, 1)

    async def test_invalid_configuration_is_never_repaired_by_starting_host(self):
        self.env.write_text("SAGASU_READ_BACKEND=invalid\n")
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.load(self.env, diagnostic=True)
        with (
            patch(
                "sagasu.host_recovery.control",
                AsyncMock(return_value={"running": False}),
            ),
            patch("sagasu.host_recovery.connect", AsyncMock()) as launch,
        ):
            result = await HostRecovery(settings).recover()
        self.assertEqual(result["next_tool"], "configuration_status")
        launch.assert_not_awaited()
