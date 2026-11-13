"""Repeat plans remain read-only and each exact slot needs its own review."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mcp import Client
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService, AgentError, Backend
from sagasu.personal import PersonalStore, PersonalDataError
from sagasu.fbs_client import SearchResult, RoomAvailability, TimeSlot, FBSError
from sagasu.repeat import repeat_plan, booking_window
from tests.test_mcp import FakeBackend, ROW


class RepeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.profile = PersonalStore(Path(self.directory.name) / "preferences.sqlite3")
        self.client = Mock()
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=False)
        self.client.book = AsyncMock()

        async def search(config, room_name=None):
            return SearchResult(
                config,
                [
                    RoomAvailability(
                        room_name,
                        [TimeSlot(config.start_time, config.end_time, "free")],
                    )
                ],
                "test",
            )

        self.client.search = AsyncMock(side_effect=search)

        class RepeatBackend(FakeBackend):
            async def run(inner, kind, payload, progress, notify):
                if kind == "repeat":
                    with patch(
                        "sagasu.mcp_service.FBSClient", return_value=self.client
                    ):
                        return await Backend().run(kind, payload, progress, notify)
                return await super().run(kind, payload, progress, notify)

        self.backend = RepeatBackend()
        self.service = AgentService(backend=self.backend, profile=self.profile)
        self.addAsyncCleanup(self.service.close)

    async def finish(self, job):
        await self.service.get(job["operation_id"]).task
        return self.service.get(job["operation_id"])

    def template(self):
        return self.profile.save_template(
            "Project meeting",
            "Room A",
            "11:00",
            "14:00",
            "Project work",
            "friend@smu.edu.sg",
        )

    async def test_template_repeat_round_trip_through_mcp_and_individual_review(self):
        async with Client(
            create_server(self.service, allow_agent_submission=True)
        ) as client:
            result = await client.call_tool(
                "save_booking_template",
                dict(
                    name="Project meeting",
                    room="Room A",
                    start_time="11:00",
                    end_time="14:00",
                    purpose="Project work",
                    co_booker="friend@smu.edu.sg",
                ),
            )
            self.assertFalse(result.is_error)
            template = result.structured_content
            response = await client.call_tool(
                "repeat_bookings",
                {"date": "2099-11-08", "template_ids": [template["id"]]},
            )
            plan = await self.finish(response.structured_content)
            self.assertEqual(plan.status, "succeeded")
            self.assertFalse(plan.result["submitted"])
            self.assertEqual(plan.result["date"], "08-Nov-2099")
            result = await client.call_tool(
                "repeat_booking_review", {"operation_id": plan.id, "slot_index": 0}
            )
            self.assertFalse(result.is_error)
            review = result.structured_content
            self.assertEqual(review["summary"]["co_booker"], "friend@smu.edu.sg")
            self.assertEqual(review["summary"]["search"]["start_time"], "11:00")
            self.assertEqual(review["summary"]["search"]["date"], "08-Nov-2099")
            self.client.book.assert_not_awaited()
            self.assertNotIn("book", [kind for kind, _ in self.backend.calls])
            response = await client.call_tool(
                "submit_review", {"review_id": review["review_id"]}
            )
            await self.finish(response.structured_content)
            self.assertEqual([kind for kind, _ in self.backend.calls].count("book"), 1)

    async def test_reference_repeat_fetches_details_and_keeps_original_times(self):
        listing = await self.finish(self.service.start("bookings"))
        source = {
            **ROW,
            **booking_window(ROW),
            "purpose": "Original purpose",
            "co_booker": "friend@smu.edu.sg",
        }
        with patch(
            "sagasu.repeat.read_booking_details", AsyncMock(return_value=source)
        ) as read:
            job = await self.finish(
                self.service.repeat_bookings(
                    "2099-11-08", listing.id, [ROW["reference_number"]]
                )
            )
        read.assert_awaited_once_with(self.client, ROW["reference_number"])
        self.assertEqual(job.result["slots"][0]["missing_fields"], [])
        review = self.service.repeat_review(job.id, 0)
        self.assertEqual(review["summary"]["room"], ROW["room"])
        self.assertEqual(review["summary"]["search"]["start_time"], "14:00")

    async def test_missing_details_are_requested_before_a_review(self):
        listing = await self.finish(self.service.start("bookings"))
        source = {**ROW, **booking_window(ROW), "purpose": None, "co_booker": None}
        with patch(
            "sagasu.repeat.read_booking_details", AsyncMock(return_value=source)
        ):
            job = await self.finish(
                self.service.repeat_bookings(
                    "2099-11-08", listing.id, [ROW["reference_number"]]
                )
            )
        self.assertEqual(
            job.result["slots"][0]["missing_fields"], ["purpose", "co_booker"]
        )
        with self.assertRaises(AgentError):
            self.service.repeat_review(job.id, 0)
        self.assertIn(
            "review_id",
            self.service.repeat_review(
                job.id, 0, purpose="Meeting", co_booker="friend@smu.edu.sg"
            ),
        )

    async def test_failed_slot_does_not_hide_successful_slot(self):
        one = self.template()
        two = self.profile.save_template(
            "Other", "Room B", "14:30", "17:30", "Study", "friend@smu.edu.sg"
        )
        original = self.client.search.side_effect

        async def search(config, room_name=None):
            if room_name == "Room A":
                raise FBSError("PRIVATE")
            return await original(config, room_name)

        self.client.search.side_effect = search
        job = await self.finish(
            self.service.repeat_bookings(
                "2099-11-08", template_ids=[one["id"], two["id"]]
            )
        )
        self.assertTrue(job.result["partial_failure"])
        self.assertEqual(
            [s["status"] for s in job.result["slots"]], ["failed", "available"]
        )
        self.assertNotIn("PRIVATE", str(job.result))
        self.assertIn("review_id", self.service.repeat_review(job.id, 1))
        with self.assertRaises(AgentError):
            self.service.repeat_review(job.id, 0)

    async def test_source_and_template_changes_do_not_rewrite_queued_plan_or_review(
        self,
    ):
        template = self.template()
        job = self.service.repeat_bookings("2099-11-08", template_ids=[template["id"]])
        self.profile.save_template(
            "Project meeting",
            "Room Changed",
            "09:00",
            "10:00",
            "Changed",
            "other@smu.edu.sg",
        )
        completed = await self.finish(job)
        review = self.service.repeat_review(completed.id, 0)
        self.assertEqual(review["summary"]["room"], "Room A")
        self.assertEqual(review["summary"]["purpose"], "Project work")
        self.profile.delete_template(template["id"])
        self.assertEqual(self.service.review(review["review_id"]), review)

    async def test_templates_survive_restart_and_invalid_requests_fail_before_browser(
        self,
    ):
        import sqlite3

        template = self.template()
        with sqlite3.connect(self.profile.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(PersonalStore(self.profile.path).templates(), [template])
        for args in [
            dict(template_ids=[template["id"]] * 2),
            dict(template_ids=["missing"]),
            dict(reference_numbers=[ROW["reference_number"]]),
            dict(template_ids=[]),
        ]:
            with self.assertRaises(AgentError):
                self.service.repeat_bookings("2099-11-08", **args)
        with self.assertRaises(PersonalDataError):
            self.profile.save_template(
                "Bad", "Room", "11:01", "14:00", "Work", "friend@smu.edu.sg"
            )
        self.assertEqual(self.client.search.await_count, 0)


class RepeatDetailsBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        await self.page.route(
            "https://fbs.intranet.smu.edu.sg/**",
            lambda route: route.fulfill(body="<body></body>", content_type="text/html"),
        )
        await self.page.goto("https://fbs.intranet.smu.edu.sg/repeat-fixture")

    async def details(self, *, actual_reference="BK-TEST", emails="friend@smu.edu.sg"):
        from sagasu.repeat import read_booking_details
        from types import SimpleNamespace

        client = SimpleNamespace(
            page=SimpleNamespace(
                frame=lambda **kw: self.page,
                wait_for_timeout=self.page.wait_for_timeout,
            ),
            _wait_for_idle=AsyncMock(),
            progress=AsyncMock(),
        )
        await self.page.set_content(
            """<table id="GridViewBooking_content"><tr class="row"><td></td><td>BK-TEST</td><td>16-Nov-2026 (Mon) 11:00 - 14:00</td><td>LKS</td><td>Room A</td><td></td><td></td><td></td><td>Confirmed</td><td><a id="row_BtnView" href="#">VIEW</a></td></tr></table>"""
        )
        await self.page.locator("#row_BtnView").evaluate(
            """(el, values)=>el.onclick=()=>{document.body.innerHTML=
            '<div id="bookingFormControl1_ObjectNumber"><span>'+values.ref+'</span></div>'+
            '<input id="bookingFormControl1_TextboxPurpose_c1" value="Project work">'+
            '<div id="bookingFormControl1_GridCoBookers"><table id="bookingFormControl1_GridCoBookers_gv"><tr><th>NAME</th></tr><tr><td>'+values.emails+'</td></tr></table><span>1 ITEM(S) TOTAL</span></div>';return false;}""",
            {"ref": actual_reference, "emails": emails},
        )
        with patch(
            "sagasu.fbs_account.read_bookings",
            AsyncMock(
                return_value={
                    "bookings": [{"reference_number": "BK-TEST"}],
                    "complete": True,
                    "total": 1,
                }
            ),
        ):
            return await read_booking_details(client, "BK-TEST")

    async def test_reads_verified_reference_purpose_and_unique_cobooker(self):
        result = await self.details()
        self.assertEqual(result["purpose"], "Project work")
        self.assertEqual(result["co_booker"], "friend@smu.edu.sg")
        self.assertEqual(result["start_time"], "11:00")

    async def test_multiple_cobookers_are_left_for_user_to_choose(self):
        result = await self.details(emails="one@smu.edu.sg two@smu.edu.sg")
        self.assertIsNone(result["co_booker"])

    async def test_wrong_reference_details_are_rejected(self):
        from sagasu.fbs_client import LayoutChanged

        with self.assertRaises(LayoutChanged):
            await self.details(actual_reference="BK-OTHER")
