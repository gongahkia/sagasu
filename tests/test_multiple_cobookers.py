"""Multiple exact directory selections retain immutable review/template snapshots."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from mcp import Client
from playwright.async_api import async_playwright
from sagasu.fbs_client import (
    BookingRequest,
    FBSClient,
    FBS_URL,
    LayoutChanged,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
)
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentError
from sagasu.personal import PersonalStore
from sagasu.repeat import verified_cobookers
from tests.test_mcp import CONFIG, ROOM, ServiceCase


class ParticipantRequestTests(unittest.TestCase):
    def test_legacy_mapping_stays_compatible_and_multiple_mapping_is_immutable(self):
        request = BookingRequest(
            SearchConfig.from_mapping(CONFIG), ROOM["name"], "Study", "one@smu.edu.sg"
        )
        self.assertNotIn("co_bookers", request.to_dict())
        participants = ("one@smu.edu.sg", "two@scis.smu.edu.sg")
        multiple = BookingRequest(
            request.search,
            request.room,
            request.purpose,
            participants[0],
            co_bookers=participants,
        )
        self.assertEqual(
            BookingRequest.from_mapping(multiple.to_dict()).participants, participants
        )
        for emails in [
            [],
            (participants[0], participants[0].upper()),
            ("bad@example.com",),
            (participants[1],),
            tuple(f"person{i}@smu.edu.sg" for i in range(9)),
        ]:
            with self.subTest(emails=emails), self.assertRaises(ValueError):
                BookingRequest(
                    request.search,
                    request.room,
                    request.purpose,
                    participants[0],
                    co_bookers=emails,
                )

    def test_repeat_requires_complete_verified_participants_and_keeps_all_of_them(self):
        details = {
            "co_bookers": [
                {"name": "Same Name", "email": "one@smu.edu.sg"},
                {"name": "Same Name", "email": "two@smu.edu.sg"},
            ],
            "co_bookers_coverage": {"complete": True},
        }
        self.assertEqual(
            verified_cobookers(details), ["one@smu.edu.sg", "two@smu.edu.sg"]
        )
        for changed in [
            {**details, "co_bookers_coverage": {"complete": False}},
            {**details, "co_bookers": [{"name": "Name", "email": None}]},
        ]:
            self.assertIsNone(verified_cobookers(changed))


class MultipleFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        self.actions = []
        await self.page.expose_function(
            "record", lambda action: self.actions.append(action)
        )
        self.drop_existing = False
        self.duplicate = False

        async def fixture(route):
            if route.request.url == FBS_URL:
                await route.fulfill(
                    body='<iframe name="frameContent" src="/form"></iframe>',
                    content_type="text/html",
                )
                return
            prefix = "bookingFormControl1"
            picker = prefix + "_DialogSearchCoBooker"
            people = ["SAME NAME (one@smu.edu.sg)", "SAME NAME (two@smu.edu.sg)"]
            html = rf"""<input id="{prefix}_TextboxPurpose_c1">
                <select id="{prefix}_DropDownUsageType_c1"><option>AdHoc</option></select>
                <select id="{prefix}_DropDownSpaceBookingUsage_c1"><option>Meeting</option></select>
                <input type="checkbox" id="{prefix}_SendCalendarInvitation_c1">
                <input type="checkbox" id="{prefix}_TermsAndConditionsCheckbox_c1">
                <a id="{prefix}_GridCoBookers_ctl14" onclick="document.getElementById('picker').hidden=false">ADD</a>
                <div id="{prefix}_GridCoBookers_content"><table id="{prefix}_GridCoBookers_gv"><tbody id="retained"></tbody></table></div>
                <div id="picker" hidden><input id="{picker}_searchPanel_textBox_c1">
                <a id="{picker}_searchPanel_buttonSearch" onclick="renderMatches()">Search</a>
                <table id="{picker}_searchPanel_gridView_gv"><tbody id="results"></tbody></table>
                <a id="{picker}_dialogBox_b1" onclick="add()">OK</a></div>
                <a id="panel_UIButton2" onclick="record('CONFIRM')">CONFIRM</a>
                <script>
                const people={json.dumps(people)};
                const retained=[];
                function renderMatches(){{
                    let found=people.filter(p=>p.includes(document.getElementById('{picker}_searchPanel_textBox_c1').value));
                    if ({str(self.duplicate).lower()}) found=found.concat(found);
                    document.getElementById('results').innerHTML=found.map(p=>`<tr><td><input type="checkbox"></td><td><a href="#">${{p}}</a></td></tr>`).join('');
                }}
                function add(){{
                    const selected=[...document.querySelectorAll('#results tr')].filter(r=>r.querySelector('input').checked).map(r=>r.innerText.trim().replace(/\s*\([^()]+\)$/,''));
                    if ({str(self.drop_existing).lower()}) retained.length=0;
                    retained.push(...selected);
                    document.getElementById('retained').innerHTML=retained.map(p=>`<tr class="row"><td><input type="checkbox"></td><td>${{p}}</td></tr>`).join('');
                    document.getElementById('picker').hidden=true;
                    record('ADD_PERSON');
                }}
                </script>"""
            await route.fulfill(body=html, content_type="text/html")

        await self.page.route("**/*", fixture)
        await self.page.goto(FBS_URL)
        self.form = self.page.frame(name="frameContent")
        await self.form.locator("#bookingFormControl1_TextboxPurpose_c1").wait_for()
        self.client = FBSClient(progress=AsyncMock())
        self.client.page = self.page
        self.client._prepare_booking_frame = AsyncMock(return_value=self.form)
        self.client._verify_booking_details = AsyncMock()
        config = SearchConfig.from_mapping(CONFIG)
        self.client.search = AsyncMock(
            return_value=SearchResult(
                config,
                [RoomAvailability(ROOM["name"], [TimeSlot("14:00", "16:00", "free")])],
                "fixture",
            )
        )
        self.request = BookingRequest(
            config,
            ROOM["name"],
            "Study",
            "one@smu.edu.sg",
            co_bookers=("one@smu.edu.sg", "two@smu.edu.sg"),
        )

    async def test_same_names_with_distinct_exact_emails_both_remain_without_final_confirmation(
        self,
    ):
        result = await self.client.book(self.request, submit=False)
        self.assertEqual(result["co_bookers"], list(self.request.participants))
        self.assertEqual(self.actions, ["ADD_PERSON", "ADD_PERSON"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(await self.form.locator("#retained tr").count(), 2)
        self.client._verify_booking_details.assert_awaited_once()

    async def test_lost_earlier_participant_fails_before_confirmation(self):
        self.drop_existing = True
        await self.form.goto(self.form.url)
        with self.assertRaisesRegex(LayoutChanged, "Not all selected co-bookers"):
            await self.client.book(self.request, submit=True)
        self.assertNotIn("CONFIRM", self.actions)
        self.client._verify_booking_details.assert_not_awaited()

    async def test_duplicate_directory_email_is_rejected_before_selecting_any_person(
        self,
    ):
        self.duplicate = True
        await self.form.goto(self.form.url)
        with self.assertRaisesRegex(LayoutChanged, "ambiguous"):
            await self.client.book(self.request, submit=True)
        self.assertEqual(self.actions, [])


class MultipleWorkflowTests(ServiceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service.profile = PersonalStore(
            Path(self.temporary.name) / "people.sqlite3"
        )

    async def test_mcp_resolves_each_contact_once_and_review_does_not_follow_contact_updates(
        self,
    ):
        person = self.service.profile.save_contact("Friend", "one@smu.edu.sg")
        self.service.profile.set_contact_aliases(person["id"], ["Pal"])
        source = await self.finish(self.service.search(CONFIG))
        async with Client(create_server(self.service)) as client:
            result = await client.call_tool(
                "booking_review",
                {
                    "search_operation_id": source.id,
                    "room": ROOM["name"],
                    "purpose": "Study",
                    "co_bookers": ["Pal", "two@SCIS.SMU.EDU.SG"],
                },
            )
            self.assertFalse(result.is_error)
            review = result.structured_content
        self.service.profile.save_contact(
            "Friend", "changed@smu.edu.sg", contact_id=person["id"]
        )
        self.assertEqual(
            self.service.review(review["review_id"])["summary"]["co_bookers"],
            ("one@smu.edu.sg", "two@scis.smu.edu.sg"),
        )
        payload = self.service.reviews[review["review_id"]]["payload"]
        self.assertEqual(
            payload.participants, ("one@smu.edu.sg", "two@scis.smu.edu.sg")
        )

    async def test_duplicates_invalid_emails_and_mixed_inputs_do_not_create_reviews(
        self,
    ):
        person = self.service.profile.save_contact("Friend", "one@smu.edu.sg")
        source = await self.finish(self.service.search(CONFIG))
        for people in [
            [],
            [person["id"], "one@smu.edu.sg"],
            ["bad@example.com"],
            [None],
            ["one@smu.edu.sg"] * 9,
        ]:
            with (
                self.subTest(people=people),
                self.assertRaises((AgentError, ValueError)),
            ):
                self.service.review_booking(
                    source.id, ROOM["name"], "Study", co_bookers=people
                )
        with self.assertRaises(AgentError):
            self.service.review_booking(
                source.id,
                ROOM["name"],
                "Study",
                co_booker="one@smu.edu.sg",
                co_bookers=["two@smu.edu.sg"],
            )
        self.assertEqual(self.service.reviews, {})

    async def test_template_restart_and_repeat_preserve_every_person(self):
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "save_booking_template",
                {
                    "name": "Pair",
                    "room": ROOM["name"],
                    "start_time": "14:00",
                    "end_time": "16:00",
                    "purpose": "Study",
                    "co_bookers": ["one@smu.edu.sg", "two@smu.edu.sg"],
                },
            )
            self.assertFalse(response.is_error)
        saved = PersonalStore(self.service.profile.path).templates()[0]
        self.assertEqual(saved["co_bookers"], ["one@smu.edu.sg", "two@smu.edu.sg"])
        # a completed repeat result is the immutable handoff, rather than a new live write.
        search = await self.finish(self.service.search(CONFIG))
        self.backend.run = AsyncMock(
            return_value={
                "slots": [
                    {
                        "source": saved,
                        "details": saved,
                        "booking_co_bookers": saved["co_bookers"],
                        "search": search.result,
                    }
                ]
            }
        )
        operation = await self.finish(self.service.start("repeat", {}))
        review = self.service.repeat_review(operation.id, 0)
        self.assertEqual(list(review["summary"]["co_bookers"]), saved["co_bookers"])
