import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp import Client

from sagasu.mcp_server import create_server
from sagasu.personal import PersonalStore, PersonalDataError
from sagasu.room_preferences import merge_preferences
from tests.test_mcp import ServiceCase, CONFIG, ROOM


class RoomPreferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "prefs.sqlite3"
        self.store = PersonalStore(self.path)

    def test_preferences_survive_restart_without_changing_contacts_or_search_defaults(
        self,
    ):
        person = self.store.save_contact("Friend", "friend@smu.edu.sg")
        search = self.store.update_preferences({"equipment": ["Projector"]})
        self.store.update_room_preferences(
            {
                "preferred_rooms": [" B "],
                "avoided_rooms": ["A"],
                "weights": {"time_preference": 3},
            }
        )
        reopened = PersonalStore(self.path)
        self.assertEqual(reopened.room_preferences()["preferred_rooms"], ["B"])
        self.assertEqual(reopened.room_preferences()["weights"]["room_preference"], 2)
        self.assertEqual(reopened.preferences(), search)
        self.assertEqual(reopened.resolve_contact(person["id"]), "friend@smu.edu.sg")

    def test_invalid_patches_leave_previous_preferences_intact(self):
        expected = self.store.update_room_preferences({"preferred_rooms": ["B"]})
        for patch in [
            {"avoided_rooms": ["b"]},
            {"preferred_rooms": ["A", "a"]},
            {"preferred_rooms": ["A\nB"]},
            {"weights": {"time_preference": True}},
            {"weights": {"time_preference": float("nan")}},
            {"weights": {"unknown": 1}},
            {"time_preference": []},
            [],
        ]:
            with self.subTest(patch=patch), self.assertRaises(PersonalDataError):
                self.store.update_room_preferences(patch)
            self.assertEqual(self.store.room_preferences(), expected)

    def test_null_resets_only_requested_fields(self):
        self.store.update_room_preferences(
            {
                "preferred_rooms": ["B"],
                "avoided_rooms": ["A"],
                "weights": {"single_room": 10},
            }
        )
        result = self.store.update_room_preferences(
            {"preferred_rooms": None, "weights": None}
        )
        self.assertEqual(result["preferred_rooms"], [])
        self.assertEqual(result["avoided_rooms"], ["A"])
        self.assertEqual(result["weights"], merge_preferences()["weights"])

    def test_concurrent_patches_do_not_lose_other_fields(self):
        self.store.room_preferences()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda patch: PersonalStore(self.path).update_room_preferences(
                        patch
                    ),
                    [{"preferred_rooms": ["B"]}, {"avoided_rooms": ["A"]}],
                )
            )
        result = self.store.room_preferences()
        self.assertEqual(result["preferred_rooms"], ["B"])
        self.assertEqual(result["avoided_rooms"], ["A"])


class RoomPreferenceWorkflowTests(ServiceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service.profile = PersonalStore(
            Path(self.temporary.name) / "prefs.sqlite3"
        )

    async def source(self):
        job = await self.finish(self.service.search(CONFIG))
        job.result["rooms"] = [
            {**copy.deepcopy(ROOM), "name": name} for name in ["A", "B", "C"]
        ]
        return job

    async def test_saved_preferences_filter_and_rank_before_pagination(self):
        self.service.profile.update_room_preferences(
            {"preferred_rooms": ["c"], "avoided_rooms": ["a"]}
        )
        job = await self.source()
        result = self.service.available_rooms(job.id, limit=1)
        self.assertEqual(result["rooms"][0]["name"], "C")
        self.assertEqual(result["available_total"], 2)
        self.assertEqual(result["excluded_by_room_preferences"], 1)
        self.assertEqual(len(job.result["rooms"]), 3)
        result = self.service.available_rooms(
            job.id, preferred_rooms=[], avoided_rooms=[]
        )
        self.assertEqual([r["name"] for r in result["rooms"]], ["A", "B", "C"])
        self.assertEqual(
            self.service.profile.room_preferences()["avoided_rooms"], ["a"]
        )

    async def test_all_excluded_rooms_do_not_trigger_more_fbs_searches(self):
        self.service.profile.update_room_preferences({"avoided_rooms": ["A", "B", "C"]})
        job = await self.source()
        result = self.service.available_rooms(job.id)
        self.assertEqual(result["suggested_action"], "review_room_preferences")
        self.assertEqual(result["next_tool"], "get_room_preferences")
        self.assertEqual(len(self.backend.calls), 1)

    async def test_mcp_changes_local_preferences_without_contacting_fbs(self):
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "set_room_preferences", {"preferences": {"preferred_rooms": ["B"]}}
            )
            self.assertFalse(response.is_error)
            self.assertEqual(response.structured_content["preferred_rooms"], ["B"])
            response = await client.call_tool("get_room_preferences", {})
            self.assertEqual(response.structured_content["preferred_rooms"], ["B"])
        self.assertEqual(self.backend.calls, [])
