import tempfile
import unittest
from pathlib import Path

from mcp import Client

from sagasu.personal import PersonalStore, PersonalDataError
from sagasu.mcp_server import create_server
from tests.test_mcp import ServiceCase, CONFIG, ROOM


class AliasStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "people.sqlite3"
        self.store = PersonalStore(self.path)
        self.person = self.store.save_contact("Friend", "friend@smu.edu.sg")

    def test_aliases_resolve_case_insensitively_after_restart_and_email_update(self):
        self.store.set_contact_aliases(self.person["id"], ["WK", "Wee Kiat"])
        reopened = PersonalStore(self.path)
        self.assertEqual(reopened.resolve_contact("wk"), "friend@smu.edu.sg")
        self.assertEqual(reopened.resolve_contact("Wee Kiat"), "friend@smu.edu.sg")
        reopened.save_contact(
            "Friend renamed", "updated@smu.edu.sg", contact_id=self.person["id"]
        )
        self.assertEqual(reopened.resolve_contact("wk"), "updated@smu.edu.sg")

    def test_shared_alias_is_ambiguous_and_never_selects_first_contact(self):
        other = self.store.save_contact("Other", "other@smu.edu.sg")
        for person in [self.person, other]:
            self.store.set_contact_aliases(person["id"], ["Friend nickname"])
        with self.assertRaises(PersonalDataError):
            self.store.resolve_contact("friend nickname")
        self.assertEqual(self.store.resolve_contact(other["id"]), "other@smu.edu.sg")

    def test_invalid_updates_do_not_replace_existing_aliases(self):
        self.store.set_contact_aliases(self.person["id"], ["WK"])
        for aliases in [
            ["wk", "WK"],
            [""],
            ["A\nB"],
            ["fake@smu.edu.sg"],
            ["x" * 41],
            None,
        ]:
            with self.subTest(aliases=aliases), self.assertRaises(PersonalDataError):
                self.store.set_contact_aliases(self.person["id"], aliases)
            self.assertEqual(self.store.resolve_contact("WK"), "friend@smu.edu.sg")

    def test_alias_values_are_sql_parameters_and_clear_delete_are_scoped(self):
        alias = "Robert'); DROP TABLE contacts;--"
        self.store.set_contact_aliases(self.person["id"], [alias])
        self.assertEqual(self.store.resolve_contact(alias), "friend@smu.edu.sg")
        self.store.set_contact_aliases(self.person["id"], [])
        self.assertEqual(
            self.store.contacts()[self.person["id"]],
            {"name": "Friend", "email": "friend@smu.edu.sg"},
        )
        self.store.set_contact_aliases(self.person["id"], ["WK"])
        self.store.delete_contact(self.person["id"])
        with self.store._db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM contact_aliases").fetchone()[0], 0
            )


class AliasWorkflowTests(ServiceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service.profile = PersonalStore(
            Path(self.temporary.name) / "people.sqlite3"
        )

    async def test_mcp_alias_change_is_local_and_existing_review_email_is_immutable(
        self,
    ):
        person = self.service.profile.save_contact("Friend", "friend@smu.edu.sg")
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "set_cobooker_aliases", {"cobooker_id": person["id"], "aliases": ["WK"]}
            )
            self.assertFalse(response.is_error)
            self.assertEqual(self.backend.calls, [])
            listing = await client.call_tool("list_cobookers", {})
            self.assertEqual(
                listing.structured_content["cobookers"][0]["aliases"], ["WK"]
            )
        source = await self.finish(self.service.search(CONFIG))
        review = self.service.review_booking(
            source.id, ROOM["name"], "Study", co_booker="wk"
        )
        self.service.profile.save_contact(
            "Friend", "updated@smu.edu.sg", contact_id=person["id"]
        )
        self.service.profile.set_contact_aliases(person["id"], ["Different"])
        self.assertEqual(
            self.service.review(review["review_id"])["summary"]["co_booker"],
            "friend@smu.edu.sg",
        )
