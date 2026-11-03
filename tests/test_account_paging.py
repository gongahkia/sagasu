"""Exercise real DOM pager controls, coverage, and reference lookup without FBS."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from sagasu.fbs_client import LayoutChanged
from sagasu.fbs_paging import collect_pages


class AccountPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_booking_reader_aggregates_pages_and_targets_reference(self):
        from sagasu.fbs_account import read_bookings

        await self.page.set_content(
            """<div id="GridViewBooking"><button title="First Page" onclick="i=0;render()">First</button>
        <div id="GridViewBooking_content"></div><span>2 ITEM(S) TOTAL</span>
        <button id="next" title="Next Page" onclick="i++;render()">Next</button></div>
        <script>let i=0;function render(){document.querySelector('#GridViewBooking_content').innerHTML=
        '<table><tr class="row"><td></td><td>BK-'+i+'</td><td>23-Nov-2026 (Mon) 11:00 - 14:00</td><td></td><td>Room A</td><td></td><td></td><td></td><td>Confirmed</td></tr></table>';
        document.querySelector('#next').disabled=i===1;}render();</script>"""
        )
        self.client.ensure_authenticated = AsyncMock()
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(return_value=self.page)
        ):
            result = await read_bookings(self.client)
            self.assertTrue(result["complete"])
            self.assertEqual(
                [row["reference_number"] for row in result["bookings"]],
                ["BK-0", "BK-1"],
            )
            result = await read_bookings(self.client, reference="BK-1")
            self.assertTrue(result["reference_found"])
            self.assertEqual(result["bookings"][0]["reference_number"], "BK-1")

    async def asyncSetUp(self):
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        self.client = SimpleNamespace(_wait_for_idle=AsyncMock())

    async def setup_grid(self, pages, total=None, *, stuck=False):
        total = sum(map(len, pages)) if total is None else total
        await self.page.set_content(
            """<div id="Grid"><button onclick="index=0;render()" title="First Page">First</button>
        <span id="rows"></span><button id="next" title="Next Page" onclick="advance()">Next</button></div>
        <script>let index=0;let pages="""
            + json.dumps(pages)
            + """;
        function render(){document.querySelector('#rows').textContent=JSON.stringify(pages[index]);
        document.querySelector('#next').disabled=index>=pages.length-1;}
        function advance(){"""
            + ("" if stuck else "index++;")
            + """render();}render();</script>"""
        )

        async def read():
            rows = json.loads(await self.page.locator("#rows").inner_text())
            count = await self.page.evaluate("window.changedTotal ?? " + str(total))
            return {
                "bookings": [{"reference_number": r} for r in rows],
                "total": count,
                "complete": len(rows) == count,
            }

        return read

    async def test_collects_all_pages_and_preserves_totals(self):
        read = await self.setup_grid([["A", "B"], ["C"], ["D", "E"]])
        result = await collect_pages(self.client, self.page, "Grid", "bookings", read)
        self.assertTrue(result["complete"])
        self.assertEqual(result["pages_read"], 3)
        self.assertEqual(
            [r["reference_number"] for r in result["bookings"]], list("ABCDE")
        )

    async def test_target_reference_leaves_only_its_page_visible_for_actions(self):
        read = await self.setup_grid([["A"], ["B", "C"], ["D"]])
        result = await collect_pages(
            self.client, self.page, "Grid", "bookings", read, reference="C"
        )
        self.assertTrue(result["reference_found"])
        self.assertEqual(
            result["bookings"], [{"reference_number": "B"}, {"reference_number": "C"}]
        )
        self.assertEqual(await self.page.evaluate("index"), 1)

    async def test_repeated_or_overlapping_pages_fail_closed(self):
        for pages, stuck in [([["A"], ["B"]], True), ([["A"], ["A"]], False)]:
            read = await self.setup_grid(pages, stuck=stuck)
            with self.assertRaises(LayoutChanged):
                await collect_pages(self.client, self.page, "Grid", "bookings", read)

    async def test_limit_and_missing_control_report_partial_coverage(self):
        read = await self.setup_grid([["A"], ["B"]])
        result = await collect_pages(
            self.client, self.page, "Grid", "bookings", read, max_pages=1
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["coverage_reason"], "page_limit")
        await self.page.locator("#next").evaluate("e=>e.removeAttribute('title')")
        await self.page.locator("#next").evaluate(
            "e=>{e.textContent='Unrecognized';e.removeAttribute('onclick')}"
        )
        result = await collect_pages(self.client, self.page, "Grid", "bookings", read)
        self.assertFalse(result["complete"])
        self.assertEqual(result["coverage_reason"], "unsupported_or_missing_next_page")

    async def test_total_changes_fail_closed(self):
        read = await self.setup_grid([["A"], ["B"]])

        async def changing():
            result = await read()
            if await self.page.evaluate("index") == 1:
                result["total"] = 3
            return result

        with self.assertRaises(LayoutChanged):
            await collect_pages(self.client, self.page, "Grid", "bookings", changing)

    async def test_starts_from_first_page_even_if_previous_read_left_later_page(self):
        read = await self.setup_grid([["A"], ["B"]])
        await self.page.locator("#next").click()
        result = await collect_pages(self.client, self.page, "Grid", "bookings", read)
        self.assertTrue(result["complete"])
        self.assertEqual(result["pages_read"], 2)
