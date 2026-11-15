"""Shortlist persistence, exact refresh scope and ordinary review handoff."""

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp import Client
from sagasu.fbs_client import FBS_TIMEZONE, LayoutChanged
from sagasu.mcp_service import AgentError, AgentService, Backend
from sagasu.mcp_server import create_server
from sagasu.personal import PersonalDataError, PersonalStore
from sagasu.room_shortlists import validate_shortlist, refresh_shortlist
from tests.test_room_plans import CONFIG, room, snapshot


NOW = datetime(2099, 11, 1, 11, tzinfo=FBS_TIMEZONE)


def payload():
    return {
        "config": copy.deepcopy(CONFIG),
        "choices": [
            {
                "date": "01-Nov-2099",
                "legs": [{"room": "A", "start_time": "12:00", "end_time": "13:00"}],
            }
        ],
    }


class ShortlistStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = PersonalStore(Path(self.temporary.name) / "plans.sqlite3")

    def test_restart_update_scope_and_delete_keep_snapshots_independent(self):
        saved = self.store.save_room_plan("Study", payload())
        restarted = PersonalStore(self.store.path)
        self.assertEqual(restarted.saved_room_plans(), [saved])
        original = restarted.saved_room_plan(saved["id"])
        changed = payload()
        changed["choices"][0]["legs"][0]["room"] = "B"
        updated = restarted.save_room_plan("Study", changed)
        self.assertEqual(updated["id"], saved["id"])
        self.assertEqual(original["plan"]["choices"][0]["legs"][0]["room"], "A")
        self.assertEqual(
            PersonalStore(self.store.path, scope="other").saved_room_plans(), []
        )
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        restarted.delete_room_plan(saved["id"])
        self.assertEqual(restarted.saved_room_plans(), [])

    def test_invalid_updates_and_corrupt_data_fail_without_reset_or_overwrite(self):
        saved = self.store.save_room_plan("Study", payload())
        invalid = payload()
        invalid["config"]["equipment"] = False
        with self.assertRaises((ValueError, PersonalDataError)):
            self.store.save_room_plan("Study", invalid)
        self.assertEqual(
            self.store.saved_room_plan(saved["id"])["plan"]["choices"],
            payload()["choices"],
        )
        with self.store._db(write=True) as db:
            db.execute(
                "UPDATE saved_room_plans SET payload=? WHERE scope=? AND id=?",
                ('{"BROKEN":true}', "local", saved["id"]),
            )
        with self.assertRaises(PersonalDataError):
            self.store.saved_room_plans()
        with self.store._db() as db:
            self.assertEqual(
                db.execute("SELECT payload FROM saved_room_plans").fetchone()[0],
                '{"BROKEN":true}',
            )

    def test_invalid_leg_bounds_dates_and_duplicate_choices_are_rejected(self):
        for change in [
            lambda p: p["choices"][0]["legs"][0].update(start_time="11:30"),
            lambda p: p["choices"][0]["legs"][0].update(room="A\nB"),
            lambda p: p["choices"].append(copy.deepcopy(p["choices"][0])),
            lambda p: p["config"].update(equipment=["Imaginary equipment"]),
            lambda p: p["choices"][0].update(date="invalid"),
        ]:
            bad = payload()
            change(bad)
            with (
                self.subTest(bad=bad),
                self.assertRaises((ValueError, PersonalDataError)),
            ):
                validate_shortlist(bad)


class ShortlistRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_read_per_date_preserves_equipment_building_and_exact_choices(
        self,
    ):
        saved = payload()
        saved["choices"] += [
            {
                "date": "01-Nov-2099",
                "legs": [{"room": "B", "start_time": "14:00", "end_time": "15:00"}],
            },
            {
                "date": "02-Nov-2099",
                "legs": [{"room": "A", "start_time": "12:00", "end_time": "13:00"}],
            },
        ]
        read = AsyncMock(
            side_effect=lambda c: snapshot(
                c,
                [
                    room("A", [("12:00", "16:00", "free")]),
                    room("B", [("12:00", "16:00", "booked")]),
                ],
            )
        )
        result = await refresh_shortlist(saved, read, now=NOW)
        self.assertEqual(read.await_count, 2)
        self.assertEqual(
            [c["status"] for c in result["shortlist"]],
            ["available", "unavailable_in_saved_scope", "available"],
        )
        self.assertEqual(len(result["candidates"]), 2)
        for call in read.await_args_list:
            config = call.args[0]
            self.assertEqual(config.equipment, ("Projector",))
            self.assertEqual(config.buildings, tuple(CONFIG["buildings"]))
        self.assertEqual(read.await_args_list[0].args[0].end_time, "15:00")
        self.assertNotIn("PRIVATE BOOKING", json.dumps(result))
        self.assertFalse(result["submitted"])

    async def test_elapsed_choices_do_not_trigger_reads_or_become_unavailable_claims(
        self,
    ):
        read = AsyncMock()
        result = await refresh_shortlist(payload(), read, now=NOW.replace(hour=13))
        read.assert_not_awaited()
        self.assertEqual(result["shortlist"][0]["status"], "elapsed")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["complete"])

    async def test_per_date_errors_keep_other_available_choices_and_never_mean_no_rooms(
        self,
    ):
        saved = payload()
        saved["choices"].append(
            {"date": "02-Nov-2099", "legs": copy.deepcopy(saved["choices"][0]["legs"])}
        )

        def read(c):
            if c.date == "02-Nov-2099":
                raise LayoutChanged("PRIVATE DETAIL")
            return snapshot(c)

        result = await refresh_shortlist(saved, AsyncMock(side_effect=read), now=NOW)
        self.assertEqual(
            [c["status"] for c in result["shortlist"]], ["available", "failed"]
        )
        self.assertFalse(result["complete"])
        self.assertTrue(result["partial_failure"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertNotIn("PRIVATE DETAIL", json.dumps(result))

    async def test_split_choice_requires_both_saved_legs_and_cancellation_propagates(
        self,
    ):
        saved = payload()
        saved["choices"][0]["legs"].append(
            {"room": "B", "start_time": "13:30", "end_time": "14:00"}
        )
        result = await refresh_shortlist(
            saved, AsyncMock(side_effect=lambda c: snapshot(c)), now=NOW
        )
        self.assertEqual(result["shortlist"][0]["status"], "unavailable_in_saved_scope")
        with self.assertRaises(asyncio.CancelledError):
            await refresh_shortlist(
                saved, AsyncMock(side_effect=asyncio.CancelledError()), now=NOW
            )


class SavedPlanWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = PersonalStore(Path(self.temporary.name) / "plans.sqlite3")
        self.backend = Backend(interactive_auth=False)
        self.backend.reads.mode = "api"
        self.backend.reads.direct = AsyncMock(
            side_effect=lambda kind, c, progress: snapshot(c)
        )
        self.service = AgentService(backend=self.backend, profile=self.profile)
        self.addAsyncCleanup(self.service.close)

    async def test_save_restart_refresh_and_review_use_only_the_exact_chosen_leg(self):
        operation = self.service.plan_rooms(
            CONFIG, dates=["2099-11-01", "2099-11-02"], mode="common"
        )
        await self.service.get(operation["operation_id"]).task
        with patch("sagasu.mcp_service.FBSClient") as browser:
            async with Client(create_server(self.service)) as client:
                saved = await client.call_tool(
                    "save_room_plan",
                    {
                        "name": "Week",
                        "operation_id": operation["operation_id"],
                        "candidate_indices": [0],
                    },
                )
                self.assertFalse(saved.is_error)
                plan_id = saved.structured_content["id"]
                await self.service.close()
                restarted = AgentService(
                    backend=self.backend, profile=PersonalStore(self.profile.path)
                )
                self.addAsyncCleanup(restarted.close)
            async with Client(create_server(restarted)) as client:
                stored = await client.call_tool(
                    "list_saved_plans", {"plan_id": plan_id}
                )
                self.assertEqual(
                    stored.structured_content["availability"],
                    "historical_snapshot_requires_refresh",
                )
                refresh = await client.call_tool(
                    "refresh_saved_plan", {"plan_id": plan_id}
                )
                job = restarted.get(refresh.structured_content["operation_id"])
                await job.task
                self.assertEqual(job.status, "succeeded")
                self.assertEqual(len(job.result["candidates"]), 2)
                review = await client.call_tool(
                    "planned_booking_review",
                    {
                        "operation_id": job.id,
                        "candidate_index": 1,
                        "purpose": "Study",
                        "co_booker": "friend@smu.edu.sg",
                    },
                )
                self.assertFalse(review.is_error)
                self.assertEqual(
                    review.structured_content["summary"]["search"]["date"],
                    "02-Nov-2099",
                )
                self.assertEqual(review.structured_content["summary"]["room"], "A")
        browser.assert_not_called()
        self.assertEqual(self.backend.reads.direct.await_count, 4)

    async def test_search_saves_no_calendar_booker_text_and_updates_cannot_rewrite_queued_refresh(
        self,
    ):
        operation = self.service.search(CONFIG)
        source = self.service.get(operation["operation_id"])
        await source.task
        saved = self.service.save_room_plan("Rooms", source.id, rooms=["A"])
        self.assertNotIn(
            "timeslots", json.dumps(self.profile.saved_room_plan(saved["id"]))
        )
        gate = asyncio.Event()

        async def blocked(kind, c, progress):
            await gate.wait()
            return snapshot(c)

        self.backend.reads.direct.side_effect = blocked
        refresh = self.service.refresh_saved_plan(saved["id"])
        self.profile.delete_room_plan(saved["id"])
        gate.set()
        job = self.service.get(refresh["operation_id"])
        await job.task
        self.assertEqual(job.result["saved_plan"]["id"], saved["id"])
        self.assertEqual(job.result["candidates"][0]["legs"][0]["room"], "A")
        self.assertEqual(self.profile.saved_room_plans(), [])

    async def test_invalid_selections_create_neither_saved_records_nor_reviews(self):
        operation = self.service.plan_rooms(CONFIG)
        await self.service.get(operation["operation_id"]).task
        for indices in [[], [True], [9999], [0, 0]]:
            with self.subTest(indices=indices), self.assertRaises(AgentError):
                self.service.save_room_plan(
                    "Bad", operation["operation_id"], candidate_indices=indices
                )
        self.assertEqual(self.profile.saved_room_plans(), [])
        self.assertEqual(self.service.reviews, {})
