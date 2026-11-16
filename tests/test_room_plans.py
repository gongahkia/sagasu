"""Deterministic planning and MCP review boundaries; no live FBS writes."""

import asyncio
import copy
import json
import unittest
import tempfile
from pathlib import Path
from dataclasses import asdict
from datetime import datetime
from unittest.mock import AsyncMock, patch

from mcp import Client

from sagasu.fbs_client import FBS_TIMEZONE, FBSError, SearchConfig
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentError, AgentService, Backend
from sagasu.personal import PersonalStore, PersonalDataError
from sagasu.room_plans import find_room_plans, prepare_plan, MAX_CANDIDATES

CONFIG = {
    "date": "01-Nov-2099",
    "start_time": "12:00",
    "end_time": "16:00",
    "buildings": ["School of Computing & Information Systems 1"],
    "facility_types": ["Group Study Room"],
    "equipment": ["Projector"],
}


def snapshot(config, rooms=None):
    return {
        "config": asdict(config),
        "rooms": (
            rooms if rooms is not None else [room("A", [("12:00", "16:00", "free")])]
        ),
        "scraped_at": "2099-01-01T12:00:00+08:00",
        "read_backend": {"name": "api"},
    }


def room(name, slots):
    return {
        "name": name,
        "timeslots": [
            {
                "start": a,
                "end": b,
                "status": state,
                "details": "PRIVATE BOOKING" if state == "booked" else None,
            }
            for a, b, state in slots
        ],
    }


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_flexible_windows_reuse_one_query_per_date_and_keep_hard_filters(
        self,
    ):
        payload = prepare_plan(
            CONFIG, dates=["2099-11-01", "2099-11-02"], duration_minutes=120
        )
        read = AsyncMock(side_effect=lambda c: snapshot(c))
        result = await find_room_plans(payload, read)
        self.assertEqual(read.await_count, 2)
        self.assertEqual(result["total_matching_candidates"], 10)
        for call in read.call_args_list:
            self.assertEqual(call.args[0].equipment, ("Projector",))
            self.assertEqual(call.args[0].start_time, "12:00")
            self.assertEqual(call.args[0].end_time, "16:00")
        self.assertTrue(result["complete"])
        self.assertFalse(result["submitted"])
        self.assertNotIn("timeslots", json.dumps(result))

    async def test_common_candidates_require_same_room_and_time_on_every_date(self):
        def read(c):
            slots = (
                [("12:00", "13:00", "booked"), ("13:00", "16:00", "free")]
                if c.date.startswith("02")
                else [("12:00", "16:00", "free")]
            )
            return snapshot(
                c,
                [
                    room("A", slots),
                    room("Other " + c.date, [("12:00", "16:00", "free")]),
                ],
            )

        payload = prepare_plan(
            CONFIG,
            dates=["2099-11-01", "2099-11-02"],
            duration_minutes=120,
            mode="common",
        )
        result = await find_room_plans(payload, AsyncMock(side_effect=read))
        self.assertEqual(result["total"], 3)
        self.assertTrue(
            all(
                c["all_dates"] and c["legs"][0]["room"] == "A"
                for c in result["candidates"]
            )
        )
        self.assertEqual(result["candidates"][0]["legs"][0]["start_time"], "13:00")
        self.assertNotIn("occurrences", json.dumps(result))

    async def test_failed_date_is_unknown_and_cannot_produce_a_common_match(self):
        read = AsyncMock(
            side_effect=[
                snapshot(SearchConfig.from_mapping(CONFIG)),
                FBSError("PRIVATE RESPONSE"),
            ]
        )
        payload = prepare_plan(
            CONFIG, dates=["2099-11-01", "2099-11-02"], mode="common"
        )
        result = await find_room_plans(payload, read)
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["complete"])
        self.assertTrue(result["partial_failure"])
        self.assertEqual(result["suggested_action"], "retry_failed_dates")
        self.assertNotIn("PRIVATE", json.dumps(result))

    async def test_any_mode_keeps_verified_candidates_from_successful_dates(self):
        read = AsyncMock(
            side_effect=[
                FBSError("PRIVATE"),
                snapshot(SearchConfig.from_mapping({**CONFIG, "date": "02-Nov-2099"})),
            ]
        )
        payload = prepare_plan(CONFIG, dates=["2099-11-01", "2099-11-02"])
        result = await find_room_plans(payload, read)
        self.assertTrue(result["partial_failure"])
        self.assertTrue(all(c["date"] == "02-Nov-2099" for c in result["candidates"]))

    async def test_adjacent_free_cells_are_one_contiguous_window(self):
        data = snapshot(
            SearchConfig.from_mapping(CONFIG),
            [
                room(
                    "A",
                    [
                        ("12:00", "13:00", "free"),
                        ("13:00", "14:00", "free"),
                        ("14:00", "16:00", "booked"),
                    ],
                )
            ],
        )
        result = await find_room_plans(
            prepare_plan(CONFIG, duration_minutes=120), AsyncMock(return_value=data)
        )
        self.assertEqual(result["total"], 1)
        self.assertNotIn("PRIVATE BOOKING", json.dumps(result))

    async def test_missing_overlapping_or_changed_scope_data_never_becomes_free(self):
        for slots in [
            [("12:00", "13:00", "free"), ("14:00", "16:00", "free")],
            [("12:00", "14:00", "free"), ("13:00", "16:00", "booked")],
            [],
        ]:
            with self.subTest(slots=slots):
                data = snapshot(SearchConfig.from_mapping(CONFIG), [room("A", slots)])
                result = await find_room_plans(
                    prepare_plan(CONFIG), AsyncMock(return_value=data)
                )
                self.assertFalse(result["complete"])
                self.assertEqual(result["candidates"], [])
        wrong = snapshot(SearchConfig.from_mapping({**CONFIG, "equipment": []}))
        result = await find_room_plans(
            prepare_plan(CONFIG), AsyncMock(return_value=wrong)
        )
        self.assertTrue(result["partial_failure"])

    async def test_split_plans_require_explicit_permission_and_two_free_legs(self):
        rooms = [
            room("A", [("12:00", "13:00", "free"), ("13:00", "16:00", "booked")]),
            room(
                "B",
                [
                    ("12:00", "13:00", "booked"),
                    ("13:00", "14:00", "free"),
                    ("14:00", "16:00", "booked"),
                ],
            ),
        ]
        read = AsyncMock(side_effect=lambda c: snapshot(c, rooms))
        without = await find_room_plans(
            prepare_plan(CONFIG, duration_minutes=120), read
        )
        self.assertEqual(without["candidates"], [])
        result = await find_room_plans(
            prepare_plan(CONFIG, duration_minutes=120, allow_split=True), read
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(
            result["candidates"][0]["legs"],
            [
                {"room": "A", "start_time": "12:00", "end_time": "13:00"},
                {"room": "B", "start_time": "13:00", "end_time": "14:00"},
            ],
        )
        self.assertEqual(result["candidates"][0]["room_changes"], 1)

    async def test_transition_gap_is_additional_to_booked_duration(self):
        rooms = [
            room("A", [("12:00", "13:00", "free"), ("13:00", "16:00", "booked")]),
            room(
                "B",
                [
                    ("12:00", "13:30", "booked"),
                    ("13:30", "14:30", "free"),
                    ("14:30", "16:00", "booked"),
                ],
            ),
        ]
        result = await find_room_plans(
            prepare_plan(
                CONFIG, duration_minutes=120, allow_split=True, transition_minutes=30
            ),
            AsyncMock(side_effect=lambda c: snapshot(c, rooms)),
        )
        self.assertEqual(result["candidates"][0]["legs"][1]["start_time"], "13:30")
        self.assertEqual(result["candidates"][0]["legs"][1]["end_time"], "14:30")

    async def test_split_is_not_offered_where_an_eligible_single_room_fits(self):
        rooms = [
            room("A", [("12:00", "13:00", "free"), ("13:00", "16:00", "booked")]),
            room("B", [("12:00", "13:00", "booked"), ("13:00", "16:00", "free")]),
            room("C", [("12:00", "16:00", "free")]),
        ]
        result = await find_room_plans(
            prepare_plan(CONFIG, duration_minutes=120, allow_split=True),
            AsyncMock(side_effect=lambda c: snapshot(c, rooms)),
        )
        self.assertTrue(all(c["room_changes"] == 0 for c in result["candidates"]))

    async def test_weights_and_avoidance_change_ranking_without_weakening_filters(self):
        rooms = [room(n, [("12:00", "16:00", "free")]) for n in ["A", "B", "C"]]
        prefs = {
            "preferred_rooms": ["B"],
            "avoided_rooms": ["C"],
            "time_preference": "latest",
            "weights": {"room_preference": 10, "time_preference": 1},
        }
        result = await find_room_plans(
            prepare_plan(CONFIG, duration_minutes=120, preferences=prefs),
            AsyncMock(side_effect=lambda c: snapshot(c, rooms)),
        )
        first = result["candidates"][0]
        self.assertEqual(first["legs"][0]["room"], "B")
        self.assertEqual(first["legs"][0]["start_time"], "14:00")
        self.assertTrue(all(c["legs"][0]["room"] != "C" for c in result["candidates"]))
        self.assertGreater(first["score_components"]["room_preference"], 0)

    async def test_matching_candidates_are_bounded_and_truncation_is_explicit(self):
        rooms = [room(f"Room {i}", [("12:00", "16:00", "free")]) for i in range(50)]
        result = await find_room_plans(
            prepare_plan(CONFIG, duration_minutes=30),
            AsyncMock(side_effect=lambda c: snapshot(c, rooms)),
        )
        self.assertEqual(result["total"], MAX_CANDIDATES)
        self.assertEqual(result["total_matching_candidates"], 400)
        self.assertTrue(result["truncated"])

    async def test_each_date_has_a_retained_pointer_even_when_absent_from_first_page(
        self,
    ):
        payload = prepare_plan(
            CONFIG, dates=["2099-11-01", "2099-11-08"], duration_minutes=120
        )
        result = await find_room_plans(
            payload,
            AsyncMock(
                side_effect=lambda c: snapshot(
                    c,
                    [
                        room(f"Room {i}", [("12:00", "16:00", "free")])
                        for i in range(10)
                    ],
                )
            ),
        )
        self.assertEqual(
            [entry["date"] for entry in result["date_options"]],
            ["01-Nov-2099", "08-Nov-2099"],
        )
        for entry in result["date_options"]:
            self.assertEqual(entry["status"], "option_found")
            chosen = result["candidates"][entry["candidate_index"]]
            self.assertEqual(chosen["date"], entry["date"])
            self.assertEqual(chosen["legs"], entry["legs"])
            self.assertEqual(entry["room_changes"], 0)
        self.assertGreater(result["date_options"][1]["candidate_index"], 0)

    async def test_a_date_omitted_by_retention_is_not_reported_unavailable(self):
        with patch("sagasu.room_plans.MAX_CANDIDATES", 1):
            result = await find_room_plans(
                prepare_plan(
                    CONFIG, dates=["2099-11-01", "2099-11-08"], duration_minutes=120
                ),
                AsyncMock(side_effect=lambda c: snapshot(c)),
            )
        self.assertTrue(result["complete"])
        self.assertTrue(result["truncated"])
        omitted = result["date_options"][1]
        self.assertEqual(omitted["status"], "candidate_not_retained")
        self.assertIsNone(omitted["candidate_index"])
        self.assertIn("same constraints", omitted["next_action"])

    async def test_explicitly_incomplete_calendar_is_not_a_complete_plan(self):
        result = await find_room_plans(
            prepare_plan(CONFIG),
            AsyncMock(side_effect=lambda c: {**snapshot(c), "complete": False}),
        )
        self.assertFalse(result["complete"])
        self.assertTrue(result["partial_failure"])
        self.assertEqual(result["date_options"][0]["status"], "failed")
        self.assertEqual(result["diagnostics_tool"], "support_bundle")

    def test_past_starts_are_trimmed_within_the_requested_window(self):
        now = datetime(2099, 11, 1, 12, 10, tzinfo=FBS_TIMEZONE)
        payload = prepare_plan(CONFIG, now=now)
        self.assertEqual(payload["configs"][0].start_time, "12:30")
        self.assertEqual(payload["config"]["start_time"], "12:00")
        late = prepare_plan(CONFIG, now=now.replace(hour=16))
        self.assertEqual(late["configs"], [None])

    async def test_computation_limit_is_explicit_without_inventing_a_total_or_best_match(
        self,
    ):
        with patch("sagasu.room_plans.MAX_EVALUATED_CANDIDATES", 2):
            result = await find_room_plans(
                prepare_plan(CONFIG), AsyncMock(side_effect=lambda c: snapshot(c))
            )
        self.assertEqual(result["matching_candidates_evaluated"], 2)
        self.assertIsNone(result["total_matching_candidates"])
        self.assertFalse(result["ranking_complete"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])

    def test_invalid_inputs_fail_before_fbs_queries(self):
        for patch in [
            {"dates": []},
            {"dates": ["2099-11-01", "01-Nov-2099"]},
            {"dates": ["2020-01-01"]},
            {"duration_minutes": True},
            {"duration_minutes": 45},
            {"duration_minutes": 300},
            {"transition_minutes": 15},
            {"mode": "common", "allow_split": True},
        ]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                prepare_plan(CONFIG, **patch)


class PlanningWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrestricted_floors_and_type_variants_are_explicit_without_changing_defaults(
        self,
    ):
        self.backend.reads.direct = AsyncMock(
            side_effect=lambda kind, config, progress: snapshot(
                config, [room("A", [(config.start_time, config.end_time, "free")])]
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            self.service.profile = PersonalStore(Path(temporary) / "personal.sqlite3")
            self.service.profile.update_preferences(
                {
                    "date_raw": "23 November 2099",
                    "start_time": "10:00",
                    "floors": ["Level 5"],
                }
            )
            before = self.service.profile.preferences()
            async with Client(create_server(self.service)) as client:
                for floor_override, expected, source in [
                    ([], [], "one_off_preferences"),
                    (None, ["Level 2", "Level 3", "Level 4"], "built_in_default"),
                ]:
                    started = await client.call_tool(
                        "plan_rooms",
                        {
                            "preferences": {
                                "buildings": ["Li Ka Shing Library"],
                                "floors": floor_override,
                                "facility_types": [
                                    "Project Room",
                                    "Project Room (Level 5)",
                                ],
                            },
                            "duration_minutes": 120,
                        },
                    )
                    self.assertFalse(started.is_error, started.content)
                    finished = (
                        await client.call_tool(
                            "wait_operation",
                            {
                                "operation_id": started.structured_content[
                                    "operation_id"
                                ]
                            },
                        )
                    ).structured_content
                    self.assertEqual(finished["status"], "succeeded", finished)
                    scope = finished["result"]["effective_scope"]
                    self.assertEqual(scope["floors"], expected)
                    self.assertEqual(scope["filter_sources"]["floors"], source)
                    self.assertEqual(
                        scope["facility_types"],
                        ["Project Room", "Project Room (Level 5)"],
                    )
                    self.assertIn("this scope", scope["notice"])
                    self.assertEqual(
                        self.backend.reads.direct.await_args.args[1].floors,
                        tuple(expected),
                    )
            self.assertEqual(before, self.service.profile.preferences())

    async def test_six_hour_preference_window_checks_each_date_once_without_saving(
        self,
    ):
        self.backend.reads.direct = AsyncMock(
            side_effect=lambda kind, config, progress: snapshot(
                config, [room("A", [(config.start_time, config.end_time, "free")])]
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            self.service.profile = PersonalStore(Path(temporary) / "personal.sqlite3")
            self.service.profile.update_preferences(
                {"duration_hrs": 3, "floors": ["Level 2"]}
            )
            before = self.service.profile.preferences()
            async with Client(create_server(self.service)) as client:
                options = (
                    await client.call_tool("search_options", {})
                ).structured_content
                self.assertIn(6, options["choices"]["duration"])
                started = await client.call_tool(
                    "plan_rooms",
                    {
                        "preferences": {"start_time": "10:00", "duration_hrs": 6},
                        "dates": ["2099-11-23", "2099-11-30"],
                        "duration_minutes": 120,
                        "mode": "common",
                    },
                )
                self.assertFalse(started.is_error, started.content)
                result = (
                    await client.call_tool(
                        "wait_operation",
                        {
                            "operation_id": started.structured_content["operation_id"],
                            "limit": 1,
                        },
                    )
                ).structured_content
                self.assertEqual(result["status"], "succeeded", result)
                self.assertTrue(result["result"]["complete"])
                self.assertEqual(result["result"]["duration_minutes"], 120)
                self.assertTrue(result["result"]["candidates"][0]["all_dates"])
                rejected = await client.call_tool(
                    "plan_rooms",
                    {"preferences": {"duration_hrs": 6.25}, "duration_minutes": 120},
                )
                self.assertTrue(rejected.is_error)
                overnight = await client.call_tool(
                    "plan_rooms",
                    {
                        "preferences": {"start_time": "22:00", "duration_hrs": 6},
                        "duration_minutes": 120,
                    },
                )
                self.assertTrue(overnight.is_error)
            self.assertEqual(self.service.profile.preferences(), before)
        self.assertEqual(self.backend.reads.direct.await_count, 2)
        for call in self.backend.reads.direct.await_args_list:
            config = call.args[1]
            self.assertEqual((config.start_time, config.end_time), ("10:00", "16:00"))
            self.assertEqual(config.floors, ("Level 2",))

    async def test_default_filter_planning_preserves_saved_scope_without_an_options_lookup_or_mutation(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            self.service.profile = PersonalStore(Path(temporary) / "personal.sqlite3")
            self.service.profile.update_preferences(
                {
                    "date_raw": "01 November 2099",
                    "start_time": "08:00",
                    "duration_hrs": 1,
                    "buildings": ["Li Ka Shing Library"],
                    "floors": ["Level 5"],
                    "equipment": ["Projector"],
                }
            )
            before = self.service.profile.preferences()
            setup = lambda: {
                "operational": True,
                "mode": "operational",
                "authentication": {},
            }
            async with Client(
                create_server(self.service, configuration_provider=setup)
            ) as client:
                status = await client.call_tool("server_status")
                self.assertEqual(
                    status.structured_content["default_search"]["buildings"],
                    ["Li Ka Shing Library"],
                )
                response = await client.call_tool(
                    "plan_rooms",
                    {
                        "preferences": {
                            "date_raw": "2099-11-02",
                            "start_time": "12:00",
                            "duration_hrs": 4,
                        },
                        "duration_minutes": 120,
                    },
                )
                self.assertFalse(response.is_error, response.content)
                job = self.service.get(response.structured_content["operation_id"])
                await job.task
                self.assertEqual(job.status, "succeeded")
                config = self.backend.reads.direct.call_args.args[1]
                self.assertEqual(
                    (config.date, config.start_time, config.end_time),
                    ("02-Nov-2099", "12:00", "16:00"),
                )
                self.assertEqual(config.buildings, ("Li Ka Shing Library",))
                self.assertEqual(config.equipment, ("Projector",))
                self.assertEqual(config.floors, ("Level 5",))
                scope = job.result["effective_scope"]
                self.assertEqual(scope["floors"], ["Level 5"])
                self.assertEqual(scope["filter_sources"]["floors"], "saved_preferences")
                self.assertIn("floors", scope["inherited_filters"])
                self.assertIn("Level 5", scope["notice"])
                self.assertEqual(
                    scope["completeness_applies_to"],
                    "these_dates_windows_and_filters_only",
                )
                response = await client.call_tool(
                    "plan_rooms",
                    {"config": CONFIG, "preferences": {}, "duration_minutes": 120},
                )
                self.assertTrue(response.is_error)
            self.assertEqual(before, self.service.profile.preferences())
            self.assertEqual(self.backend.reads.direct.await_count, 1)

    async def test_invalid_saved_defaults_do_not_hide_server_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.service.profile = PersonalStore(Path(temporary) / "personal.sqlite3")
            setup = lambda: {
                "operational": True,
                "mode": "operational",
                "authentication": {},
            }
            with patch.object(
                self.service,
                "get_preferences",
                side_effect=PersonalDataError("PRIVATE"),
            ):
                async with Client(
                    create_server(self.service, configuration_provider=setup)
                ) as client:
                    status = await client.call_tool("server_status")
            self.assertFalse(status.is_error)
            self.assertIsNone(status.structured_content["default_search"])
            self.assertNotIn("PRIVATE", json.dumps(status.structured_content))

    async def asyncSetUp(self):
        self.backend = Backend(interactive_auth=False)
        self.backend.reads.mode = "api"
        self.backend.reads.direct = AsyncMock(
            side_effect=lambda kind, c, progress: snapshot(c)
        )
        self.service = AgentService(backend=self.backend)
        self.addAsyncCleanup(self.service.close)

    async def test_mcp_planning_paginates_and_reviews_only_the_selected_date_and_leg(
        self,
    ):
        with patch("sagasu.mcp_service.FBSClient") as browser:
            async with Client(create_server(self.service)) as client:
                response = await client.call_tool(
                    "plan_rooms",
                    {
                        "config": CONFIG,
                        "duration_minutes": 60,
                        "dates": ["2099-11-01", "2099-11-02"],
                        "mode": "common",
                    },
                )
                self.assertFalse(response.is_error)
                op = response.structured_content["operation_id"]
                await self.service.get(op).task
                result = self.service.result(op, offset=1, limit=1)
                self.assertTrue(result["result"]["candidates"][0]["all_dates"])
                index = result["result"]["candidates"][0]["candidate_index"]
                review = await client.call_tool(
                    "planned_booking_review",
                    {
                        "operation_id": op,
                        "candidate_index": index,
                        "occurrence_index": 1,
                        "purpose": "Study",
                        "co_booker": "friend@smu.edu.sg",
                    },
                )
                self.assertFalse(review.is_error)
                details = review.structured_content["summary"]
                self.assertEqual(details["search"]["date"], "02-Nov-2099")
                self.assertEqual(
                    details["search"]["start_time"],
                    result["result"]["candidates"][0]["legs"][0]["start_time"],
                )
        browser.assert_not_called()
        self.assertEqual(self.backend.reads.direct.await_count, 2)
        self.assertEqual(len(self.service.reviews), 1)
        self.assertEqual(len(self.service.jobs), 1)

    async def test_invalid_plan_and_review_indices_never_invoke_backend_or_create_reviews(
        self,
    ):
        for config in [
            {**CONFIG, "capacity": "unknown"},
            {**CONFIG, "equipment": ["Imaginary equipment"]},
        ]:
            with self.assertRaises((AgentError, ValueError)):
                self.service.plan_rooms(config)
        self.backend.reads.direct.assert_not_awaited()
        first = self.service.plan_rooms(CONFIG)
        await self.service.get(first["operation_id"]).task
        for indexes in [(-1, 0, 0), (False, 0, 0), (999, 0, 0), (0, 1, 0), (0, 0, 1)]:
            with self.assertRaises(AgentError):
                self.service.planned_booking_review(
                    first["operation_id"],
                    *indexes,
                    purpose="Study",
                    co_booker="friend@smu.edu.sg",
                )
        self.assertEqual(self.service.reviews, {})

    async def test_cancelled_planning_does_not_start_more_date_queries(self):
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()

        self.backend.reads.direct.side_effect = blocked
        first = self.service.plan_rooms(CONFIG, dates=["2099-11-01", "2099-11-02"])
        await entered.wait()
        result = await self.service.cancel(first["operation_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.backend.reads.direct.await_count, 1)
