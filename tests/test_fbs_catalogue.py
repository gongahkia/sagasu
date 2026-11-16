"""Native facility/credit reads verify labels and exclude unrelated profile data."""

import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright
from sagasu.fbs_catalogue import (
    parse_credits,
    parse_room_details,
    read_credit_status,
    read_room_details,
)
from sagasu.fbs_client import FBSClient, FBS_URL, LayoutChanged
from sagasu.mcp_service import AgentError
from tests.test_mcp import ServiceCase, Client

if Client is not None:
    from sagasu.mcp_server import create_server


def room_snapshot():
    return {
        "Building": "BUILDING: SCIS 1",
        "ResourceType": "FACILITY TYPE: Group Study Room",
        "ResourceName": "FACILITY: Room A",
        "Capacity": "CAPACITY: 6",
        "Description": "DESCRIPTION:",
        "equipment": "",
        "photo_available": True,
        "hours": {
            "headers": [
                "PERIOD",
                "TYPE",
                "MON",
                "TUE",
                "WED",
                "THU",
                "FRI",
                "SAT",
                "SUN",
                "PH",
            ],
            "rows": [
                ["Term Period", "Hourly", "08:30 - 22:30", "", "", "", "", "", "", ""]
            ],
            "summary": "1 ITEM(S) TOTAL",
        },
    }


class CatalogueParserTests(unittest.TestCase):
    def test_period_hours_and_missing_fields_do_not_invent_eligibility_or_absence(self):
        parsed = parse_room_details(room_snapshot(), "Room A")
        self.assertEqual(parsed["capacity"], 6)
        self.assertEqual(parsed["operating_hours"][0]["mon"], "08:30 - 22:30")
        self.assertIsNone(parsed["description"])
        self.assertIsNone(parsed["accessibility"])
        self.assertIsNone(parsed["equipment_details"])
        self.assertEqual(parsed["equipment_coverage"], "not_exposed")
        self.assertTrue(parsed["photo_available"])
        self.assertNotIn("bookable", parsed)

    def test_present_equipment_text_does_not_claim_a_complete_inventory(self):
        snapshot = room_snapshot()
        snapshot["equipment"] = "Built In Equipment\nProjector\n1 ITEM(S) TOTAL"
        parsed = parse_room_details(snapshot, "Room A")
        self.assertIn("Projector", parsed["equipment_details"])
        self.assertEqual(parsed["equipment_coverage"], "displayed_text_only")

    def test_identity_missing_labels_bad_capacity_and_partial_hours_fail_closed(self):
        for key, value in [
            ("ResourceName", "FACILITY: Room B"),
            ("Capacity", "CAPACITY: Unknown"),
            ("Building", "Unknown: SCIS 1"),
            ("ResourceType", None),
        ]:
            with self.subTest(key=key), self.assertRaises(LayoutChanged):
                parse_room_details({**room_snapshot(), key: value}, "Room A")
        snapshot = room_snapshot()
        snapshot["hours"]["summary"] = "4 ITEM(S) TOTAL"
        with self.assertRaises(LayoutChanged):
            parse_room_details(snapshot, "Room A")

    def test_zero_credit_is_valid_and_precision_is_retained(self):
        parsed = parse_credits(
            {
                "year_start_credits": "TOTAL AVAILABLE CREDITS AT START OF YEAR: 4,500.00",
                "available_credits": "TOTAL AVAILABLE CREDITS TO USE: 0.00",
                "email": "PRIVATE",
            }
        )
        self.assertEqual(parsed["year_start_credits"], "4500.00")
        self.assertEqual(parsed["available_credits"], "0.00")
        self.assertEqual(parsed["unit"], "FBS credits")
        self.assertIsNone(parsed["quota_limits"])
        self.assertNotIn("email", parsed)
        for value in [
            "Unknown",
            "NaN",
            "4500",
            "4,50.00",
            "Infinity",
            "12.000",
            "12.00 credits",
        ]:
            with self.subTest(value=value), self.assertRaises(LayoutChanged):
                parse_credits(
                    {
                        "year_start_credits": "TOTAL AVAILABLE CREDITS AT START OF YEAR: 4500.00",
                        "available_credits": "TOTAL AVAILABLE CREDITS TO USE: " + value,
                    }
                )


class CatalogueBrowserTests(unittest.IsolatedAsyncioTestCase):
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
        self.details = room_snapshot()
        self.extra_detail = ""

        async def fixture(route):
            path = route.request.url.split("/")[-1]
            if path == "home":
                html = '<div id="menu_userprofilemenu">User</div><a href="#" id="menu_linkEditProfile" onclick="frames.frameBottom.location=\'/profile\'">Profile</a><iframe name="frameBottom" src="/blank"></iframe><iframe name="frameContent" src="/search"></iframe>'
            elif path == "search":
                html = "".join(
                    f'<input id="{control}_textValue" value="inherited filter">'
                    for control in [
                        "DropMultiBuildingList_c1",
                        "DropMultiFloorList_c1",
                        "DropMultiFacilityTypeList_c1",
                        "DropMultiEquipmentList_c1",
                    ]
                )
                html += "<select id=\"DropCapacity_c1\"><option>Any</option><option selected>10</option></select><table id=\"GridResults_gv\"><tr><td><a href=\"#\" onclick=\"let f=document.createElement('iframe');f.name='FrameEdit';f.src='/details';parent.document.body.append(f);record('details')\">Room A</a></td></tr></table>"
            elif path == "details":
                html = "".join(
                    f'<div id="{key}">{value}</div>'
                    for key, value in self.details.items()
                    if key
                    in [
                        "Building",
                        "ResourceType",
                        "ResourceName",
                        "Capacity",
                        "Description",
                    ]
                )
                html += self.extra_detail
                html += (
                    '<span id="PanelBuiltInEquipment"></span><div id="GridOperatingHours"><table><tr>'
                    + "".join(f"<th>{h}</th>" for h in self.details["hours"]["headers"])
                    + '</tr></table><div id="GridOperatingHours_content"><table><tr class="row">'
                    + "".join(f"<td>{v}</td>" for v in self.details["hours"]["rows"][0])
                    + "</tr></table></div>1 ITEM(S) TOTAL</div><button onclick=\"record('BOOK')\">Book</button>"
                )
            elif path == "profile":
                html = '<div id="LabelCreditsAtYearStart">TOTAL AVAILABLE CREDITS AT START OF YEAR: 4500.00</div><div id="LabelBookingCredits">TOTAL AVAILABLE CREDITS TO USE: 3783.38</div><input value="PRIVATE EMAIL"><div>PRIVATE CONTACT</div><button onclick="record(\'SAVE\')">Save Profile</button>'
            else:
                html = "Blank"
            await route.fulfill(body=html, content_type="text/html")

        await self.page.route("**/*", fixture)
        await self.page.goto(FBS_URL)
        self.client = FBSClient(progress=AsyncMock())
        self.client.page = self.page
        self.client.ensure_authenticated = AsyncMock(
            return_value=(self.page, self.page.frame(name="frameContent"))
        )
        self.client._set_multi_select = AsyncMock()
        self.client._set_facility_keyword = AsyncMock()

    async def test_facility_lookup_clears_inherited_filters_and_never_requests_availability_or_booking(
        self,
    ):
        result = await read_room_details(self.client, "Room A", include_image=True)
        self.assertEqual(result["capacity"], 6)
        self.assertEqual(result["receipt_kind"], "facility_details")
        self.assertFalse(result["photo_available"])
        self.assertEqual(result["receipt_scope"], "native_facility_details_page")
        self.assertIn("not a verified room photograph", result["receipt_note"])
        self.assertIn("cropped", result["receipt_note"])
        self.assertTrue(result["receipt_png"].startswith(b"\x89PNG"))
        self.assertEqual(self.actions, ["details"])
        self.assertEqual(self.client._set_multi_select.await_count, 4)
        self.client._set_facility_keyword.assert_awaited_once_with(
            self.page.frame(name="frameContent"), "Room A"
        )
        self.assertEqual(
            await self.page.frame(name="frameContent")
            .locator("#DropCapacity_c1")
            .input_value(),
            "Any",
        )

    async def test_profile_read_only_returns_labelled_credits_without_exporting_or_saving_contact_fields(
        self,
    ):
        result = await read_credit_status(self.client)
        self.assertEqual(result["available_credits"], "3783.38")
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(self.actions, [])

    async def test_wrong_room_fails_before_any_detail_or_booking_action(self):
        with self.assertRaises(LayoutChanged):
            await read_room_details(self.client, "Room B")
        self.assertEqual(self.actions, [])

    async def test_facility_image_refuses_unexpected_authentication_fields(self):
        self.extra_detail = '<input type="password" value="PRIVATE PASSWORD">'
        with self.assertRaises(LayoutChanged):
            await read_room_details(self.client, "Room A", include_image=True)
        self.assertEqual(self.actions, ["details"])


@unittest.skipIf(Client is None, "Install Sagasu for MCP protocol tests")
class CatalogueProtocolTests(ServiceCase):
    async def test_read_tools_route_as_jobs_and_invalid_room_names_fail_before_queueing(
        self,
    ):
        async with Client(create_server(self.service)) as client:
            for tool, arguments, kind in [
                ("room_details", {"room": "Room A"}, "room_details"),
                ("credit_status", {}, "credits"),
            ]:
                response = await client.call_tool(tool, arguments)
                self.assertFalse(response.is_error)
                await self.finish(response.structured_content)
                self.assertEqual(self.backend.calls[-1][0], kind)
        self.assertEqual(self.service.reviews, {})
        for room in [None, "", "Room\nA", "x" * 201]:
            with self.subTest(room=room), self.assertRaises(AgentError):
                self.service.room_details(room)
