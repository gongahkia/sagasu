"""Directory search parsing, browser side effects and MCP review handoff."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from sagasu.cobookers import (
    GRID,
    CobookerLookupUnavailable,
    parse_candidates,
    search_cobookers,
    validate_query,
)
from sagasu.fbs_client import (
    FBSClient,
    FBS_URL,
    BookingUnavailable,
    RoomAvailability,
    SearchConfig,
    SearchResult,
    TimeSlot,
)
from sagasu.mcp_service import AgentError
from tests.test_mcp import Client, CONFIG, ROOM, ServiceCase

if Client is not None:
    from sagasu.mcp_server import create_server


def snapshot(rows, total=None):
    return {
        "headers": ["", "Name"],
        "rows": [["", row] for row in rows],
        "summary": f"{len(rows) if total is None else total} ITEM(S) TOTAL",
    }


class DirectoryParserTests(unittest.TestCase):
    def test_same_names_with_distinct_emails_remain_separate_candidates(self):
        result = parse_candidates(
            snapshot(
                [
                    "FRIEND NAME (one@computing.smu.edu.sg)",
                    "FRIEND NAME (two@SCIS.SMU.EDU.SG)",
                ]
            )
        )
        self.assertEqual(
            result["candidates"],
            [
                {"name": "FRIEND NAME", "email": "one@computing.smu.edu.sg"},
                {"name": "FRIEND NAME", "email": "two@scis.smu.edu.sg"},
            ],
        )

    def test_empty_results_require_explicit_zero_coverage(self):
        self.assertEqual(parse_candidates(snapshot([])), {"candidates": [], "total": 0})
        empty = snapshot([])
        empty["rows"] = [["No items"]]
        self.assertEqual(parse_candidates(empty)["candidates"], [])
        for invalid in [None, {}, snapshot([], total=4)]:
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(CobookerLookupUnavailable),
            ):
                parse_candidates(invalid)

    def test_unknown_email_domains_duplicates_and_changed_layout_fail_closed(self):
        valid = "FRIEND (friend@smu.edu.sg)"
        for data in [
            snapshot(["FRIEND (friend@example.com)"]),
            snapshot(["FRIEND (friend@computing.smu.edu.sg.example.com)"]),
            snapshot(["Name without a verified email"]),
            snapshot([valid, valid]),
            snapshot([valid], total=0),
            {**snapshot([valid]), "headers": ["", "Unknown"]},
        ]:
            with self.subTest(data=data), self.assertRaises(CobookerLookupUnavailable):
                parse_candidates(data)

    def test_query_validation_rejects_empty_wildcard_and_multiline_searches(self):
        self.assertEqual(validate_query("  Wee   Kiat  "), "Wee Kiat")
        self.assertEqual(validate_query("Ng"), "Ng")
        for query in [None, "", " ", "x", "*%", "Friend\nName", "x" * 101]:
            with self.subTest(query=query), self.assertRaises(ValueError):
                validate_query(query)


class DirectoryBrowserTests(unittest.IsolatedAsyncioTestCase):
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
        self.people = [f"FRIEND {i} (friend{i}@computing.smu.edu.sg)" for i in range(6)]

        async def fixture(route):
            if route.request.url == FBS_URL:
                await route.fulfill(
                    body='<iframe name="frameContent" src="/picker-fixture"></iframe>',
                    content_type="text/html",
                )
                return
            prefix = "bookingFormControl1_DialogSearchCoBooker"
            html = f"""<a id="bookingFormControl1_GridCoBookers_ctl14" onclick="document.querySelector('#picker').hidden=false">ADD</a>
                <div id="picker" hidden>
                <span id="{prefix}_searchPanel_textBox">
                <input placeholder="Name" onclick="this.hidden=true;document.getElementById('{prefix}_searchPanel_textBox_c1').hidden=false">
                <input id="{prefix}_searchPanel_textBox_c1" hidden></span>
                <a id="{prefix}_searchPanel_buttonSearch" onclick="pageIndex=0;render()">Search</a>
                <div id="{GRID}"></div>
                <a id="{prefix}_dialogBox_b1" onclick="record('add')">OK</a>
                </div><button onclick="record('confirm')">CONFIRM</button>
                <script>
                let pageIndex = 0;
                const people = {json.dumps(self.people)};
                function render() {{
                    const query = document.getElementById('{prefix}_searchPanel_textBox_c1').value.toLowerCase();
                    const matches = people.filter(p => p.toLowerCase().includes(query));
                    const rows = matches.slice(pageIndex*2,pageIndex*2+2).map(p =>
                        `<tr class="row"><td><input type="checkbox" onchange="record('select')"></td><td><a onclick="record('select')">${{p}}</a></td></tr>`).join('');
                    document.getElementById('{GRID}').innerHTML =
                        `<table id="duplicate"><tr><th></th><th>Name</th></tr></table><table id="duplicate">${{rows}}</table><span>${{matches.length}} ITEM(S) TOTAL</span>` +
                        `<button onclick="pageIndex=0;render()">First</button>` +
                        (pageIndex*2+2 < matches.length ? `<button onclick="pageIndex++;render()">Next</button>` : '');
                }}
                </script>"""
            await route.fulfill(body=html, content_type="text/html")

        await self.page.route("**/*", fixture)
        await self.page.goto(FBS_URL)
        self.form = self.page.frame(name="frameContent")
        self.client = FBSClient(progress=AsyncMock())
        self.client.page = self.page
        self.config = SearchConfig.from_mapping(CONFIG)
        result = SearchResult(
            self.config,
            [RoomAvailability(ROOM["name"], [TimeSlot("14:00", "16:00", "free")])],
            "fixture",
        )
        self.client.search = AsyncMock(return_value=result)
        self.client._prepare_booking_frame = AsyncMock(return_value=self.form)

    async def test_partial_name_reads_all_pages_without_selecting_or_submitting(self):
        result = await search_cobookers(
            self.client, self.config, ROOM["name"], "friend"
        )
        self.assertEqual(len(result["candidates"]), 6)
        self.assertEqual(result["total"], 6)
        self.assertTrue(result["complete"])
        self.assertEqual(result["pages_read"], 3)
        self.assertEqual(result["source"], "fbs_directory")
        self.assertEqual(self.actions, [])
        self.assertEqual(self.page.url, FBS_URL)
        self.client._prepare_booking_frame.assert_awaited_once_with(
            self.form, self.config, ROOM["name"]
        )

    async def test_no_match_is_distinct_from_an_unreadable_directory(self):
        result = await search_cobookers(
            self.client, self.config, ROOM["name"], "No match"
        )
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["complete"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(self.actions, [])

    async def test_page_budget_returns_explicit_partial_coverage(self):
        self.people.extend(f"FRIEND {i} (friend{i}@smu.edu.sg)" for i in range(6, 12))
        await self.form.goto(self.form.url)
        result = await search_cobookers(
            self.client, self.config, ROOM["name"], "friend"
        )
        self.assertEqual(len(result["candidates"]), 10)
        self.assertEqual(result["total"], 12)
        self.assertFalse(result["complete"])
        self.assertEqual(result["coverage_reason"], "page_limit")
        self.assertEqual(self.actions, [])

    async def test_unreadable_identity_does_not_become_no_matches_and_leaves_form(self):
        self.people[:] = ["FRIEND (not-an-email)"]
        await self.form.goto(self.form.url)
        with self.assertRaises(CobookerLookupUnavailable):
            await search_cobookers(self.client, self.config, ROOM["name"], "friend")
        self.assertEqual(self.page.url, FBS_URL)
        self.assertEqual(self.actions, [])

    async def test_stale_availability_never_opens_the_picker(self):
        self.client.search.return_value.rooms = []
        with self.assertRaises(BookingUnavailable):
            await search_cobookers(self.client, self.config, ROOM["name"], "friend")
        self.client._prepare_booking_frame.assert_not_awaited()
        self.assertEqual(self.actions, [])

    async def test_untrusted_frame_is_rejected_before_searching_a_name(self):
        self.client._prepare_booking_frame.return_value = SimpleNamespace(
            url="https://example.com/picker"
        )
        self.client._search_co_booker_picker = AsyncMock()
        with self.assertRaises(CobookerLookupUnavailable):
            await search_cobookers(self.client, self.config, ROOM["name"], "friend")
        self.client._search_co_booker_picker.assert_not_awaited()
        self.assertEqual(self.page.url, FBS_URL)

    async def test_cancelling_lookup_leaves_the_form_without_confirming(self):
        self.client._search_co_booker_picker = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        with self.assertRaises(asyncio.CancelledError):
            await search_cobookers(self.client, self.config, ROOM["name"], "friend")
        self.assertEqual(self.page.url, FBS_URL)
        self.assertEqual(self.actions, [])


@unittest.skipIf(Client is None, "Install Sagasu for MCP protocol tests")
class DirectoryProtocolTests(ServiceCase):
    async def test_unreadable_directory_is_an_explicit_redacted_failure(self):
        source = await self.finish(self.service.search(CONFIG))
        self.backend.error = CobookerLookupUnavailable("PRIVATE DIRECTORY CONTENT")
        async with Client(create_server(self.service)) as client:
            response = await client.call_tool(
                "search_cobookers",
                {
                    "query": "Friend",
                    "search_operation_id": source.id,
                    "room": ROOM["name"],
                },
            )
            job = response.structured_content
            await self.finish(job)
            status = self.service.result(job["operation_id"])
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"]["reason"], "cobooker_lookup_unavailable")
            self.assertNotIn("result", status)
            self.assertNotIn("PRIVATE", json.dumps(status))
            self.assertEqual(self.service.reviews, {})

    async def test_lookup_without_saved_contacts_hands_chosen_email_to_review(self):
        source = await self.finish(self.service.search(CONFIG))
        candidates = [
            {"name": "FRIEND NAME", "email": "one@computing.smu.edu.sg"},
            {"name": "FRIEND NAME", "email": "two@scis.smu.edu.sg"},
        ]
        self.backend.run = AsyncMock(
            return_value={"candidates": candidates, "total": 2, "complete": True}
        )
        async with Client(create_server(self.service)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertTrue(tools["search_cobookers"].annotations.read_only_hint)
            self.assertFalse(tools["search_cobookers"].annotations.destructive_hint)
            response = await client.call_tool(
                "search_cobookers",
                {
                    "query": "Friend",
                    "search_operation_id": source.id,
                    "room": ROOM["name"],
                },
            )
            job = response.structured_content
            await self.finish(job)
            response = await client.call_tool(
                "wait_operation", {"operation_id": job["operation_id"], "limit": 1}
            )
            result = response.structured_content["result"]
            self.assertEqual(result["candidates"], candidates[:1])
            self.assertEqual(result["pagination"]["next_offset"], 1)
            self.assertEqual(result["pagination"]["source_total"], 2)
            self.assertEqual(self.service.reviews, {})
            self.assertIsNone(self.service.profile)
            review = self.service.review_booking(
                source.id, ROOM["name"], "Study", candidates[0]["email"]
            )
            self.assertEqual(review["summary"]["co_booker"], candidates[0]["email"])
            self.assertEqual(self.backend.run.await_count, 1)
            self.assertEqual(self.backend.run.call_args.args[0], "cobooker_search")

    async def test_bad_queries_and_unsearched_rooms_do_not_start_directory_jobs(self):
        source = await self.finish(self.service.search(CONFIG))
        self.backend.run = AsyncMock()
        for query in ["", "x", "*%", "Name\nName"]:
            with self.subTest(query=query), self.assertRaises(AgentError):
                self.service.search_cobookers(source.id, ROOM["name"], query)
        with self.assertRaises(AgentError):
            self.service.search_cobookers(source.id, "Unsearched room", "Friend")
        with self.assertRaises(AgentError):
            self.service.search_cobookers("missing", ROOM["name"], "Friend")
        self.backend.run.assert_not_awaited()
