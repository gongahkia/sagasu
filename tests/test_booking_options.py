"""Live-form option reads and explicit classification snapshots, without writes."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import Client
from playwright.async_api import async_playwright
from sagasu.booking_options import parse_options, read_booking_options, validate_usage
from sagasu.fbs_client import (
    FBSClient,
    FBS_URL,
    BookingUnavailable,
    LayoutChanged,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
)
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentError
from tests.test_mcp import CONFIG, ROOM, ServiceCase


class BookingOptionParserTests(unittest.TestCase):
    def test_categories_selected_values_and_zero_credit_are_preserved(self):
        snapshot = {
            "usage_types": ["Academic", "AdHoc"],
            "booking_usages": ["Research", "Meeting"],
            "selected_usage_type": "AdHoc",
            "selected_booking_usage": "Research",
            "estimated_credits": "0.00",
        }
        result = parse_options(snapshot)
        self.assertEqual(result["estimated_credits"], "0.00")
        self.assertEqual(result["selected_booking_usage"], "Research")
        snapshot["estimated_credits"] = "1,234.50"
        self.assertEqual(parse_options(snapshot)["estimated_credits"], "1234.50")
        snapshot.pop("estimated_credits")
        self.assertIsNone(parse_options(snapshot)["estimated_credits"])

    def test_empty_changed_or_inconsistent_options_and_amounts_fail_closed(self):
        base = {"usage_types": ["AdHoc"], "booking_usages": ["Meeting"]}
        for patch in [
            {"usage_types": []},
            {"booking_usages": ["Meeting", "Meeting"]},
            {"selected_usage_type": "Unknown"},
            {"estimated_credits": "NaN"},
            {"estimated_credits": "4.5"},
            {"estimated_credits": "1,23.00"},
        ]:
            with self.subTest(patch=patch), self.assertRaises(LayoutChanged):
                parse_options({**base, **patch})
        for usage, category in [
            ("", "Meeting"),
            ("AdHoc", "a\nb"),
            (False, "Meeting"),
            ("AdHoc", "x" * 101),
        ]:
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                validate_usage(usage, category)


class BookingOptionBrowserTests(unittest.IsolatedAsyncioTestCase):
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

        async def fixture(route):
            html = (
                '<iframe name="frameContent" src="/form"></iframe>'
                if route.request.url == FBS_URL
                else """<select id="bookingFormControl1_DropDownUsageType_c1"><option value=""></option><option value="academic">Academic</option><option value="adhoc" selected>AdHoc</option><option value="disabled" disabled>Unavailable</option></select><select id="bookingFormControl1_DropDownSpaceBookingUsage_c1"><option value=""></option><option value="research">Research</option><option value="meeting" selected>Meeting</option></select><span id="bookingFormControl1_BookingCreditsDeducted_Label_c1">14.00</span><button onclick="record('ADD_PERSON')">ADD</button><button onclick="record('CONFIRM')">CONFIRM</button>"""
            )
            await route.fulfill(body=html, content_type="text/html")

        await self.page.route("**/*", fixture)
        await self.page.goto(FBS_URL)
        self.form = self.page.frame(name="frameContent")
        await self.form.locator("select").first.wait_for()
        self.client = FBSClient(progress=AsyncMock())
        self.client.page = self.page
        self.client._prepare_booking_frame = AsyncMock(return_value=self.form)
        self.config = SearchConfig.from_mapping(CONFIG)
        self.client.search = AsyncMock(
            return_value=SearchResult(
                self.config,
                [RoomAvailability(ROOM["name"], [TimeSlot("14:00", "16:00", "free")])],
                "fixture",
            )
        )

    async def test_reads_actual_enabled_choices_and_cost_then_discards_form_without_people_or_confirmation(
        self,
    ):
        result = await read_booking_options(self.client, self.config, ROOM["name"])
        self.assertEqual(result["usage_types"], ["Academic", "AdHoc"])
        self.assertEqual(result["booking_usages"], ["Research", "Meeting"])
        self.assertEqual(result["estimated_credits"], "14.00")
        self.assertEqual(
            result["config"],
            {
                **CONFIG,
                "buildings": (),
                "floors": (),
                "facility_types": (),
                "capacity": None,
                "equipment": (),
            },
        )
        self.assertFalse(result["submitted"])
        self.assertEqual(self.actions, [])
        self.assertEqual(self.page.url, FBS_URL)

    async def test_stale_availability_does_not_open_any_booking_form(self):
        self.client.search.return_value.rooms = []
        with self.assertRaises(BookingUnavailable):
            await read_booking_options(self.client, self.config, ROOM["name"])
        self.client._prepare_booking_frame.assert_not_awaited()
        self.assertEqual(self.actions, [])

    async def test_cancellation_or_untrusted_form_leaves_without_confirmation(self):
        for form in [
            SimpleNamespace(url="https://example.com/form"),
            asyncio.CancelledError(),
        ]:
            if isinstance(form, BaseException):
                self.client._prepare_booking_frame.side_effect = form
            else:
                self.client._prepare_booking_frame.return_value = form
            with (
                self.subTest(form=form),
                self.assertRaises(
                    asyncio.CancelledError
                    if isinstance(form, BaseException)
                    else LayoutChanged
                ),
            ):
                await read_booking_options(self.client, self.config, ROOM["name"])
        self.assertEqual(self.page.url, FBS_URL)
        self.assertEqual(self.actions, [])

    async def test_native_postback_resets_to_default_are_detected_before_confirmation(
        self,
    ):
        await self.form.set_content(
            """<div>Room A</div><input id="bookingFormControl1_TextboxPurpose_c1" value="Research"><select id="bookingFormControl1_DropDownStartTime_c1"><option value="11/1/2099 2:00:00 PM">14:00</option></select><select id="bookingFormControl1_DropDownEndTime_c1"><option value="11/1/2099 4:00:00 PM">16:00</option></select><select id="bookingFormControl1_DropDownUsageType_c1"><option>AdHoc</option></select><select id="bookingFormControl1_DropDownSpaceBookingUsage_c1"><option>Research</option><option>Meeting</option></select>"""
        )
        from sagasu.fbs_client import BookingRequest

        request = BookingRequest(
            self.config,
            "Room A",
            "Research",
            "friend@smu.edu.sg",
            booking_usage="Research",
        )
        await self.client._verify_booking_details(self.form, request)
        await self.form.locator(
            "#bookingFormControl1_DropDownSpaceBookingUsage_c1"
        ).select_option(label="Meeting")
        with self.assertRaisesRegex(BookingUnavailable, "classification"):
            await self.client._verify_booking_details(self.form, request)
        await self.form.locator(
            "#bookingFormControl1_DropDownSpaceBookingUsage_c1"
        ).select_option(label="Research")
        await self.form.locator("#bookingFormControl1_TextboxPurpose_c1").fill(
            "Changed"
        )
        with self.assertRaisesRegex(BookingUnavailable, "purpose"):
            await self.client._verify_booking_details(self.form, request)
        self.assertEqual(self.actions, [])


class BookingOptionWorkflowTests(ServiceCase):
    async def test_mcp_review_freezes_explicit_classifications_for_backend(self):
        source = await self.finish(self.service.search(CONFIG))
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "booking_review",
                {
                    "search_operation_id": source.id,
                    "room": ROOM["name"],
                    "purpose": "Research",
                    "co_booker": "friend@smu.edu.sg",
                    "usage_type": "AdHoc",
                    "booking_usage": "Research",
                },
            )
            self.assertFalse(response.is_error)
            review = response.structured_content
            self.assertEqual(review["summary"]["booking_usage"], "Research")
            self.assertEqual(review["summary"]["usage_type"], "AdHoc")
            validation = self.service.validate(review["review_id"])
            await self.finish(validation)
            self.assertEqual(self.backend.calls[-1][1].booking_usage, "Research")
        for category in ["", "Multi\nLine", "x" * 101]:
            with self.subTest(category=category), self.assertRaises(AgentError):
                self.service.review_booking(
                    source.id,
                    ROOM["name"],
                    "Study",
                    "friend@smu.edu.sg",
                    booking_usage=category,
                )

    async def test_native_option_read_requires_a_verified_free_search_room(self):
        source = await self.finish(self.service.search(CONFIG))
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "booking_options",
                {"search_operation_id": source.id, "room": ROOM["name"]},
            )
            self.assertFalse(response.is_error)
            await self.finish(response.structured_content)
        self.assertEqual(self.backend.calls[-1][0], "booking_options")
        self.assertEqual(self.service.reviews, {})
        with self.assertRaises(AgentError):
            self.service.booking_options(source.id, "Other Room")
