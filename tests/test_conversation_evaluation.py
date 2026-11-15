import asyncio
import copy
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mcp import Client

from benchmarks.agent_fixture import FixtureBackend, calendar
from benchmarks.conversations import (
    DATASET,
    SCHEMA,
    grade,
    parse_events,
    run_case,
    main,
)
from sagasu.fbs_client import BookingOutcomeUnknown, FBSError, SearchConfig
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService


CASES = {case["id"]: case for case in json.loads(DATASET.read_text())["cases"]}


class GradingTests(unittest.TestCase):
    def test_model_runs_require_explicit_opt_in(self):
        with (
            patch("benchmarks.conversations.evaluate") as evaluate,
            self.assertRaises(SystemExit),
        ):
            main(["--output", "/tmp/unused-agent-report.json"])
        evaluate.assert_not_called()

    def test_cli_usage_includes_cached_inputs_and_retains_reasoning_without_double_count(
        self,
    ):
        value = parse_events(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 90,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 12,
                    },
                }
            )
        )
        self.assertEqual(value["reported_total_tokens"], 120)
        self.assertEqual(value["reasoning_output_tokens"], 12)
        for events in [
            [],
            [{"type": "turn.failed"}],
            [{"type": "turn.completed", "usage": {"output_tokens": 5}}],
        ]:
            with self.assertRaises(ValueError):
                parse_events("\n".join(map(json.dumps, events)))

    def test_independent_grader_rejects_hallucinated_gaps_wrong_dates_and_unauthorized_writes(
        self,
    ):
        legs = [
            {
                "date": "2099-11-02",
                "room": "SCIS1 GSR 2-1",
                "start_time": "12:00",
                "end_time": "14:00",
            },
            {
                "date": "2099-11-02",
                "room": "SCIS1 GSR 2-2",
                "start_time": "14:00",
                "end_time": "16:00",
            },
        ]
        result = {"decision": "available", "legs": legs}
        self.assertFalse(
            grade(
                CASES["split_rooms"],
                result,
                [{"kind": "search", "date": "02-Nov-2099"}],
            )
        )
        for field, value in [
            ("room", "INVENTED ROOM"),
            ("start_time", "14:30"),
            ("date", "2099-11-03"),
        ]:
            wrong = copy.deepcopy(result)
            wrong["legs"][1][field] = value
            self.assertTrue(
                grade(
                    CASES["split_rooms"],
                    wrong,
                    [{"kind": "search", "date": "02-Nov-2099"}],
                )
            )
        self.assertTrue(
            grade(
                CASES["split_rooms"],
                result,
                [{"kind": "search"}, {"kind": "native_write"}],
            )
        )
        self.assertTrue(grade(CASES["split_rooms"], result, []))

    def test_pending_unknown_and_disambiguation_cannot_be_reported_as_confirmed(self):
        self.assertIn(
            "wrong_booking_target_or_participant",
            grade(
                CASES["duplicate_submission"],
                {"decision": "pending"},
                [{"kind": "native_write", "target_matches_request": False}],
            ),
        )
        for name in [
            "failed_search",
            "ambiguous_person",
            "unknown_submission",
            "duplicate_submission",
        ]:
            with self.subTest(name=name):
                self.assertTrue(grade(CASES[name], {"decision": "confirmed"}, []))


class FixtureProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_clarification_can_reuse_a_directory_match_verified_in_the_previous_turn(
        self,
    ):
        calls = 0

        async def create(*command, **kwargs):
            nonlocal calls
            index = calls
            calls += 1
            config = tomllib.loads(command[command.index("-c") + 1])
            args = config["mcp_servers"]["sagasu"]["args"]
            trace = Path(args[args.index("--trace") + 1])
            final = Path(command[command.index("--output-last-message") + 1])

            async def communicate(prompt):
                if index == 0:
                    trace.write_text(json.dumps({"kind": "cobooker_search"}) + "\n")
                final.write_text(
                    json.dumps(
                        {
                            "decision": "clarify" if index == 0 else "verified",
                            "answer": (
                                "Two verified Alex matches: fixture.person.2023@scis.smu.edu.sg and fixture.person.2024@scis.smu.edu.sg"
                                if index == 0
                                else "The 2023 match is the first person."
                            ),
                            "email": (
                                None
                                if index == 0
                                else "fixture.person.2023@scis.smu.edu.sg"
                            ),
                            "legs": [],
                        }
                    )
                )
                return (
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        }
                    ).encode(),
                    b"",
                )

            return Mock(pid=12345, returncode=0, communicate=communicate)

        with patch(
            "benchmarks.conversations.asyncio.create_subprocess_exec",
            side_effect=create,
        ):
            result = await run_case(
                CASES["ambiguous_person"], Path("/tmp/snapshot"), "2099-11-01", 10
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["follow_up_decision"], "verified")
        self.assertEqual(result["reported_total_tokens"], 30)

    async def test_cancellation_stops_only_the_spawned_cli_process_group(self):
        process = Mock(pid=12345, returncode=None)
        process.communicate = AsyncMock(
            side_effect=[asyncio.CancelledError(), (b"", b"")]
        )
        with (
            patch(
                "benchmarks.conversations.asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ),
            patch("benchmarks.conversations.os.killpg") as kill,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_case(
                    CASES["flexible_time"], Path("/tmp/snapshot"), "2099-11-01", 10
                )
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[0], 12345)

    async def test_fixture_uses_real_planner_and_rejects_all_unimplemented_native_paths(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FixtureBackend("flexible_time", Path(temporary) / "trace.jsonl")
            service = AgentService(backend=backend)
            self.addAsyncCleanup(service.close)
            async with Client(create_server(service)) as client:
                response = await client.call_tool(
                    "plan_rooms",
                    {
                        "config": {
                            "date": "2099-11-02",
                            "start_time": "12:00",
                            "end_time": "16:00",
                        },
                        "duration_minutes": 120,
                    },
                )
                job = service.get(response.structured_content["operation_id"])
                await job.task
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(len(job.result["candidates"]), 2)
            for kind in (
                "login",
                "booking_options",
                "room_details",
                "approve_task",
                "amend",
            ):
                with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                    await backend.run(kind, None, AsyncMock(), AsyncMock())

    async def test_synthetic_failed_search_and_lost_response_remain_real_error_types(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace"
            with self.assertRaises(FBSError):
                await FixtureBackend("failed_search", trace).run(
                    "search",
                    SearchConfig("02-Nov-2099", "12:00", "14:00"),
                    AsyncMock(),
                    AsyncMock(),
                )
            with self.assertRaises(BookingOutcomeUnknown):
                await FixtureBackend("unknown_submission", trace).run(
                    "book", None, AsyncMock(), AsyncMock()
                )
            self.assertEqual(
                sum(
                    json.loads(line)["kind"] == "native_write"
                    for line in trace.read_text().splitlines()
                ),
                1,
            )
