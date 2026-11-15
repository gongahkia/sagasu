import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sagasu.benchmark import (
    BenchmarkFailure,
    Harness,
    PhaseCapture,
    save_report,
    summarize,
    run_benchmark,
)
from sagasu.cli import main
from sagasu.config import Settings
from sagasu.mcp_service import Backend


class BenchmarkTests(unittest.TestCase):
    def test_summary_keeps_failures_separate_and_does_not_invent_tail_statistics(self):
        samples = [
            {"status": "success", "elapsed_ms": 10},
            {"status": "failed", "elapsed_ms": 50},
            {"status": "success", "elapsed_ms": 20},
        ]
        result = summarize(samples)
        self.assertEqual(result["median_ms"], 15)
        self.assertEqual(result["warm_median_ms"], 20)
        self.assertEqual(result["failures"], 1)
        self.assertIsNone(result["p95_ms"])
        self.assertIsNotNone(summarize(samples[:1] * 20)["p95_ms"])

    def test_live_opt_in_is_checked_before_loading_settings(self):
        with patch("sagasu.cli.Settings.load") as load, self.assertRaises(SystemExit):
            main(["benchmark"])
        load.assert_not_called()

    def test_equipment_control_can_choose_a_room_type_with_positive_matches(self):
        from sagasu.cli import parser

        args = parser().parse_args(
            [
                "benchmark",
                "--live",
                "--equipment-facility-type",
                "Classroom",
                "--equipment-facility-type",
                "Project Room",
            ]
        )
        self.assertEqual(args.equipment_facility_type, ["Classroom", "Project Room"])

    def test_reports_are_written_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            save_report(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            save_report(path, {"ok": False})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class LiveBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_harness_rejects_all_write_tools_before_dispatch(self):
        client = SimpleNamespace(call_tool=AsyncMock())
        harness = Harness(client, None, PhaseCapture(), 1)
        for tool in [
            "submit_review",
            "confirm_review",
            "save_cobooker",
            "save_room_plan",
            "delete_saved_plan",
            "set_room_preferences",
            "set_cobooker_aliases",
            "delete_cobooker",
            "set_search_preferences",
            "login",
        ]:
            with self.subTest(tool=tool), self.assertRaises(BenchmarkFailure):
                await harness.call(tool)
        client.call_tool.assert_not_awaited()

    async def test_benchmark_backend_disables_interactive_authentication(self):
        backend = Backend(interactive_auth=False)
        context = AsyncMock()
        context.__aenter__.return_value.ensure_authenticated = AsyncMock()
        with patch("sagasu.mcp_service.FBSClient", return_value=context) as factory:
            await backend.run("session", None, AsyncMock(), AsyncMock())
        self.assertFalse(factory.call_args.kwargs["interactive"])

    async def test_report_never_copies_private_tool_payloads(self):
        client = SimpleNamespace(call_tool=AsyncMock())
        harness = Harness(client, None, PhaseCapture(), 1)

        async def operation():
            return {
                "candidates": [
                    {"name": "PRIVATE PERSON", "email": "private@smu.edu.sg"}
                ],
                "purpose": "PRIVATE PURPOSE",
                "cookie": "SECRET",
                "complete": True,
            }

        result = await harness.measure("directory", operation)
        self.assertEqual(result["measurements"][0]["counts"], {"candidates": 1})
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_task_detail_benchmark_also_disables_interactive_auth(self):
        with patch(
            "sagasu.mcp_service.execute_task_action", AsyncMock(return_value={})
        ) as action:
            await Backend(interactive_auth=False).run(
                "task_details", {}, AsyncMock(), AsyncMock()
            )
        self.assertFalse(action.call_args.kwargs["interactive"])

    async def test_selected_case_uses_private_history_and_checkpoints_without_network(
        self,
    ):
        from tests.test_mcp import FakeBackend

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / ".env"
            env.write_text(
                f"SAGASU_BROWSER_PROFILE={root}/browser\nSAGASU_PREFERENCES_PATH={root}/prefs.sqlite3\nSAGASU_JOURNAL_PATH={root}/history.sqlite3\n"
            )
            backend = FakeBackend()
            backend.reads = SimpleNamespace(mode="auto")
            output = root / "report.json"
            with (
                patch.dict(os.environ, {}),
                patch("sagasu.benchmark.Backend", return_value=backend),
            ):
                report = await run_benchmark(
                    Settings.load(env),
                    repetitions=1,
                    date="2099-11-01",
                    start="14:00",
                    end="16:00",
                    selected=["action_history"],
                    output=output,
                )
            self.assertTrue(report["ok"])
            self.assertEqual([c["name"] for c in report["cases"]], ["action_history"])
            self.assertTrue(
                all(
                    s["mcp_calls"] == 1 and s["structured_response_bytes"] > 0
                    for s in report["cases"][0]["measurements"]
                )
            )
            self.assertFalse(json.loads(output.read_text())["in_progress"])
            self.assertFalse((root / "prefs.sqlite3").exists())
            self.assertFalse((root / "history.sqlite3").exists())
