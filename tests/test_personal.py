"""Private agent preferences, contacts, transactions and immutable booking reviews."""

import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch


from sagasu.config import Settings
from sagasu.mcp_service import AgentService, AgentError
from sagasu.personal import PersonalDataError, PersonalStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private/personal.sqlite3"
        self.store = PersonalStore(self.path)

    def test_contacts_survive_restart_and_case_insensitive_email_upsert(self):
        first = self.store.save_contact("Friend", "Friend@scis.smu.edu.sg")
        updated = PersonalStore(self.path).save_contact(
            "New name", "friend@scis.smu.edu.sg"
        )
        self.assertEqual(first["id"], updated["id"])
        self.assertEqual(
            self.store.contacts(),
            {first["id"]: {"name": "New name", "email": "friend@scis.smu.edu.sg"}},
        )
        self.store.save_contact(
            "Changed address", "other@smu.edu.sg", contact_id=first["id"]
        )
        self.assertEqual(self.store.resolve_contact(first["id"]), "other@smu.edu.sg")
        self.store.delete_contact(first["id"])
        self.assertEqual(PersonalStore(self.path).contacts(), {})

    def test_contact_validation_and_missing_ids_do_not_change_data(self):
        for name, email in (
            ("", "a@smu.edu.sg"),
            ("A\nB", "a@smu.edu.sg"),
            ("x" * 41, "a@smu.edu.sg"),
            ("A", "a@example.com"),
        ):
            with self.assertRaises(PersonalDataError):
                self.store.save_contact(name, email)
        with self.assertRaises(PersonalDataError):
            self.store.save_contact("A", "a@smu.edu.sg", contact_id="missing")
        self.assertEqual(self.store.contacts(), {})

    def test_ambiguous_names_require_ids_and_duplicate_email_updates_fail(self):
        a = self.store.save_contact("Friend", "a@smu.edu.sg")
        b = self.store.save_contact("Friend", "b@smu.edu.sg")
        with self.assertRaises(PersonalDataError):
            self.store.resolve_contact("friend")
        with self.assertRaises(PersonalDataError):
            self.store.save_contact("B", "a@smu.edu.sg", contact_id=b["id"])
        self.assertEqual(self.store.resolve_contact(a["id"]), "a@smu.edu.sg")

    def test_preference_patch_null_and_reset_preserve_contacts(self):
        person = self.store.save_contact("Friend", "friend@smu.edu.sg")
        self.store.update_preferences(
            {"floors": ["Level 2"], "duration_hrs": 3, "start_time": "09:00"}
        )
        self.store.update_preferences({"start_time": None})
        self.assertEqual(
            self.store.preferences(), {"floors": ["Level 2"], "duration_hrs": 3}
        )
        self.store.update_preferences({}, reset=True)
        self.assertEqual(self.store.preferences(), {})
        self.assertIn(person["id"], self.store.contacts())

    def test_invalid_preference_batch_rolls_back_atomically(self):
        self.store.update_preferences({"floors": ["Level 2"]})
        for change in (
            {"cookie": "secret"},
            {"floors": ["invented"]},
            {"duration_hrs": True},
            {"duration_hrs": float("nan")},
            {"room_capacity": []},
            {"start_time": "11:01"},
            {"start_time": "23:30", "duration_hrs": 4},
        ):
            with self.assertRaises(PersonalDataError):
                self.store.update_preferences(change)
        self.assertEqual(self.store.preferences(), {"floors": ["Level 2"]})

    def test_concurrent_connections_patch_fields_without_lost_updates(self):
        self.store.preferences()
        with ThreadPoolExecutor(max_workers=3) as executor:
            jobs = [
                executor.submit(
                    PersonalStore(self.path, self.store.scope).update_preferences,
                    change,
                )
                for change in (
                    {"floors": ["Level 2"]},
                    {"duration_hrs": 3},
                    {"room_capacity": "LessThan5Pax"},
                )
            ]
            for job in jobs:
                job.result()
        self.assertEqual(
            self.store.preferences(),
            {"floors": ["Level 2"], "duration_hrs": 3, "room_capacity": "LessThan5Pax"},
        )

    def test_corrupt_store_is_not_silently_reset(self):
        self.path.parent.mkdir()
        original = b"not a sqlite file; private data"
        self.path.write_bytes(original)
        with self.assertRaises(PersonalDataError) as raised:
            self.store.save_contact("Friend", "friend@smu.edu.sg")
        self.assertNotIn("private data", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), original)

    def test_private_permissions_and_unknown_schema_version(self):
        self.store.preferences()
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(PersonalDataError):
            self.store.save_contact("Friend", "friend@smu.edu.sg")


class AgentPreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "personal.sqlite3"
        self.service = AgentService(
            profile=PersonalStore(path, "local"),
            backend=NS(run=AsyncMock(return_value={})),
        )
        self.addAsyncCleanup(self.service.close)

    async def test_mcp_books_saved_contact_by_id_and_snapshots_the_review(self):
        from tests.test_mcp import CONFIG, ROOM

        person = self.service.profile.save_contact("Friend", "friend@smu.edu.sg")
        result = {"config": CONFIG, "rooms": [ROOM], "scraped_at": "test"}
        self.service.backend.run.return_value = result
        job = self.service.search(CONFIG)
        await self.service.get(job["operation_id"]).task
        review = self.service.review_booking(
            job["operation_id"], ROOM["name"], "Meeting", cobooker_id=person["id"]
        )
        self.service.profile.save_contact(
            "New name", "other@smu.edu.sg", contact_id=person["id"]
        )
        self.service.profile.update_preferences({"duration_hrs": 4})
        snapshot = self.service.review(review["review_id"])["summary"]
        self.assertEqual(snapshot["co_booker"], "friend@smu.edu.sg")
        self.assertEqual(snapshot["search"]["end_time"], "16:00")
        self.assertEqual(
            self.service.review_booking(
                job["operation_id"], ROOM["name"], "Meeting", "New name"
            )["summary"]["co_booker"],
            "other@smu.edu.sg",
        )
        with self.assertRaises(AgentError):
            self.service.review_booking(
                job["operation_id"],
                ROOM["name"],
                "Meeting",
                "friend@smu.edu.sg",
                person["id"],
            )

    async def test_explicit_search_config_does_not_overwrite_preferences(self):
        from tests.test_mcp import CONFIG

        self.service.profile.update_preferences({"duration_hrs": 3})
        operation = self.service.search(CONFIG)
        await self.service.get(operation["operation_id"]).task
        self.assertEqual(self.service.profile.preferences(), {"duration_hrs": 3})


class ConfigTests(unittest.TestCase):
    def test_preferences_path_resolves_relative_to_env_file(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            env = Path(directory) / ".env"
            env.write_text("SAGASU_PREFERENCES_PATH=private/preferences.sqlite3\n")
            settings = Settings.load(env)
            self.assertEqual(
                settings.preferences,
                (env.parent / "private/preferences.sqlite3").resolve(),
            )
            settings.activate()
            self.assertEqual(
                Path(os.environ["SAGASU_PREFERENCES_PATH"]), settings.preferences
            )
