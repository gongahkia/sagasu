"""Real FBS-style VIEW DOMs, nested task forms, pagination and excluded notes."""

import html
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from sagasu.booking_details import (
    ADDITIONAL_FIELDS,
    FIELDS,
    HISTORY_GRID,
    HISTORY_SNAPSHOT,
    read_open_booking_details,
    read_booking_details,
    snapshot_booking_details,
)
from sagasu.fbs_client import LayoutChanged
from tests.test_booking_details import RECORD, snapshot


def detail_html(
    *,
    prefix="bookingFormControl1_",
    reference=None,
    history=True,
    nested_audit=False,
    participants_total=1,
    participant_pager=False,
    bad_history=False,
):
    data = snapshot()
    if reference:
        data["fields"]["reference_number"][0]["value"] = "Ref Number: " + reference
    fields = []
    for key, definition in {**FIELDS, **ADDITIONAL_FIELDS}.items():
        if key not in data["fields"]:
            continue
        field_id = (
            "AuditComponent_"
            if nested_audit and key in {"created_by", "modified_by"}
            else prefix
        ) + definition[0]
        fields.append(
            f'<div id="{field_id}"><div>{html.escape(data["fields"][key][0]["value"])}</div></div>'
        )
    fields.append(
        f"""<div id="{prefix}GridCoBookers"><table id="{prefix}GridCoBookers_gv"><tr><th></th><th>NAME</th></tr></table><div id="{prefix}GridCoBookers_content"><table id="{prefix}GridCoBookers_gv"><tr><td><input type="checkbox" checked></td><td>First Co Booker</td></tr></table></div><div id="{prefix}GridCoBookers_pager">{participants_total} ITEM(S) TOTAL</div>"""
    )
    if participant_pager:
        fields.append(
            f"""<button type="button" onclick="document.getElementById('{prefix}GridCoBookers_content').querySelector('td:last-child').innerText='Second Co Booker';this.disabled=true">Next</button>"""
        )
    fields.append(
        '</div><input type="hidden" id="privateDirectoryId" value="PRIVATE_INTERNAL_ID">'
    )
    if history:
        action_header = "OTHER" if bad_history else "ACTION"
        fields.append(
            f"""<a id="objectBase_ButtonViewWorkflowHistory" href="#" onclick="document.getElementById('objectBase_ActivityHistoryDialogBox_p1').style.display='block';return false">Workflow History</a><div id="objectBase_ActivityHistoryDialogBox_p1" style="display:none"><a id="objectBase_ActivityHistoryDialogBox_bc" href="#" onclick="this.parentElement.style.display='none';return false">[X Close]</a><div id="{HISTORY_GRID}"><table id="{HISTORY_GRID}_gv"><tr><th>{action_header}</th><th>USER</th><th>DATE/TIME</th><th>COMMENTS</th></tr></table><div id="{HISTORY_GRID}_content"><table id="{HISTORY_GRID}_gv"><tr><td>Confirm</td><td>Recorded Confirmer</td><td>13-Nov-2026 10:30</td><td>PRIVATE_WORKFLOW_COMMENT</td></tr></table></div><div id="{HISTORY_GRID}_pager">1 ITEM(S) TOTAL</div></div></div>"""
        )
    fields.append(
        """<a id="panel_datalistWorkflowButtons_ctl02_buttonWorkflowAction" href="#" onclick="window.approvals++;return false">Confirm</a><script>window.approvals=0;</script>"""
    )
    return "<body>" + "".join(fields) + "</body>"


class BookingDetailBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        await self.page.route(
            "https://fbs.intranet.smu.edu.sg/**",
            lambda route: route.fulfill(body="<body></body>", content_type="text/html"),
        )
        await self.page.goto("https://fbs.intranet.smu.edu.sg/booking-detail-fixture")
        self.client = SimpleNamespace(
            page=SimpleNamespace(
                frame=lambda **kwargs: self.page,
                frames=[self.page],
                wait_for_timeout=self.page.wait_for_timeout,
            ),
            _wait_for_idle=AsyncMock(),
            progress=AsyncMock(),
        )

    async def test_readonly_fields_and_workflow_history_never_click_confirmation_action(
        self,
    ):
        await self.page.set_content(detail_html())
        result = await read_open_booking_details(self.client, RECORD)
        self.assertEqual(result["purpose"], "Project: α research meeting")
        self.assertEqual(result["confirmed_by"][0]["name"], "Recorded Confirmer")
        self.assertEqual(await self.page.evaluate("window.approvals"), 0)
        self.assertFalse(
            await self.page.locator(
                "#objectBase_ActivityHistoryDialogBox_p1"
            ).is_visible()
        )
        self.assertNotIn("PRIVATE_WORKFLOW_COMMENT", json.dumps(result))
        self.assertNotIn("PRIVATE_INTERNAL_ID", json.dumps(result))

    async def test_nested_task_ids_and_global_audit_are_bound_to_requested_reference(
        self,
    ):
        await self.page.set_content(
            detail_html(
                prefix="GridViewBookingDetails_gv_ctl02_bookingFormControl1_",
                nested_audit=True,
            )
        )
        result = await read_open_booking_details(self.client, RECORD)
        self.assertTrue(result["reference_verified"])
        self.assertEqual(result["booked_by"]["name"], "Original Booker")
        self.assertEqual(result["booked_by_source"], "workflow_object_audit")
        self.assertTrue(result["approval_history"]["complete"])

    async def test_wrong_reference_is_rejected_before_any_history_or_action_click(self):
        await self.page.set_content(detail_html(reference="BK-OTHER"))
        with self.assertRaises(LayoutChanged):
            await read_open_booking_details(self.client, RECORD)
        self.assertEqual(await self.page.evaluate("window.approvals"), 0)
        self.assertFalse(
            await self.page.locator(
                "#objectBase_ActivityHistoryDialogBox_p1"
            ).is_visible()
        )

    async def test_participant_pagination_collects_names_without_selecting_checkboxes(
        self,
    ):
        await self.page.set_content(
            detail_html(history=False, participants_total=2, participant_pager=True)
        )
        result = await read_open_booking_details(self.client, RECORD)
        self.assertEqual(
            [person["name"] for person in result["co_bookers"]],
            ["First Co Booker", "Second Co Booker"],
        )
        self.assertTrue(result["co_bookers_coverage"]["complete"])
        self.assertIsNone(result["co_booker"])
        self.assertEqual(await self.page.evaluate("window.approvals"), 0)

    async def test_unknown_history_layout_is_explicit_and_does_not_hide_core_metadata(
        self,
    ):
        await self.page.set_content(detail_html(bad_history=True))
        result = await read_open_booking_details(self.client, RECORD)
        self.assertEqual(result["field_status"]["approval_history"], "unreadable")
        self.assertEqual(result["booked_by"]["name"], "Original Booker")
        self.assertIsNone(result["confirmed_by"])
        self.assertFalse(
            await self.page.locator(
                "#objectBase_ActivityHistoryDialogBox_p1"
            ).is_visible()
        )

    async def test_comment_cells_are_not_extracted_from_browser_at_all(self):
        await self.page.set_content(detail_html())
        await self.page.locator("#objectBase_ButtonViewWorkflowHistory").click()
        raw = await self.page.evaluate(HISTORY_SNAPSHOT, HISTORY_GRID)
        self.assertEqual(raw["rows"][0][-1], "")
        self.assertNotIn("PRIVATE_WORKFLOW_COMMENT", json.dumps(raw))

    async def test_multiple_booking_forms_never_share_an_unbound_global_history(self):
        await self.page.set_content(
            detail_html()
            + '<div id="other_bookingFormControl1_ObjectNumber">Ref Number: BK-OTHER</div>'
        )
        result = await read_open_booking_details(self.client, RECORD)
        self.assertEqual(result["field_status"]["approval_history"], "ambiguous_scope")
        self.assertIsNone(result["approval_history"])
        self.assertFalse(
            await self.page.locator(
                "#objectBase_ActivityHistoryDialogBox_p1"
            ).is_visible()
        )

    async def test_matching_reference_on_untrusted_origin_is_rejected(self):
        await self.page.route(
            "https://other.invalid/**",
            lambda route: route.fulfill(body=detail_html(), content_type="text/html"),
        )
        await self.page.goto("https://other.invalid/booking-detail-fixture")
        with self.assertRaises(LayoutChanged):
            await read_open_booking_details(self.client, RECORD)
        with patch(
            "sagasu.fbs_account.read_bookings",
            AsyncMock(return_value={"bookings": [RECORD]}),
        ):
            with self.assertRaises(LayoutChanged):
                await read_booking_details(
                    self.client, RECORD["reference_number"], source="bookings"
                )
        self.assertEqual(await self.page.evaluate("window.approvals"), 0)

    async def test_view_lookup_reads_booked_for_from_verified_header_without_guessing(
        self,
    ):
        await self.page.set_content(
            """<div id="GridViewBooking"><table><tr><th></th><th>Ref. Number</th><th>Date &amp; Time of Use</th><th>Building</th><th>Facility</th><th>Booked For</th><th>Use Type</th><th>Charges / Credits</th><th>Status</th><th></th></tr><tbody id="GridViewBooking_content"><tr class="row"><td></td><td>BK-TEST-1</td><td>16-Nov-2026 (Mon) 10:00 - 12:00 (2hrs)</td><td>LKS</td><td>LKS Project Room 5-1</td><td>Named Beneficiary</td><td>Study</td><td>12.00</td><td>Confirmed</td><td><a id="target_BtnView" href="#">VIEW</a></td></tr></tbody></table></div><script>window.approvals=0;</script>"""
        )
        await self.page.locator("#target_BtnView").evaluate(
            "(link,html)=>link.onclick=()=>{document.body.innerHTML=html;return false}",
            detail_html(history=False),
        )
        with patch(
            "sagasu.fbs_account.read_bookings",
            AsyncMock(
                return_value={"bookings": [RECORD], "complete": True, "total": 1}
            ),
        ):
            result = await read_booking_details(
                self.client, RECORD["reference_number"], source="bookings"
            )
        self.assertEqual(
            result["additional_details"]["booked_for"]["name"], "Named Beneficiary"
        )
        self.assertEqual(result["booked_by"]["name"], "Original Booker")
        self.assertEqual(await self.page.evaluate("window.approvals"), 0)

    async def test_task_receipt_is_withheld_if_history_comments_remain_visible(self):
        from sagasu.fbs_tasks import capture_task_details

        await self.page.set_content(detail_html())
        await self.page.locator("#" + HISTORY_GRID).evaluate(
            """grid => {
            const wrapper = document.createElement('div'); wrapper.id='separateHistoryBody'; wrapper.style.display='none';
            grid.parentElement.insertAdjacentElement('afterend',wrapper);wrapper.append(grid);
            document.getElementById('objectBase_ButtonViewWorkflowHistory').onclick=()=>{
                document.getElementById('objectBase_ActivityHistoryDialogBox_p1').style.display='block';wrapper.style.display='block';return false;};
            document.getElementById('objectBase_ActivityHistoryDialogBox_bc').onclick=()=>{
                document.getElementById('objectBase_ActivityHistoryDialogBox_p1').style.display='none';return false;};
        }"""
        )
        task = {
            **RECORD,
            "status": "Pending Approval",
            "requested_by": "Original Booker",
            "request_type": "Booking Approval",
            "can_view": True,
        }
        row = SimpleNamespace(
            locator=lambda selector: SimpleNamespace(click=AsyncMock())
        )
        with patch(
            "sagasu.fbs_tasks.locate_reviewed_task",
            AsyncMock(return_value=(self.page, task, row)),
        ):
            result = await capture_task_details(self.client, task)
        self.assertNotIn("receipt_png", result)
        self.assertIn("receipt_error", result)
        self.assertNotIn("PRIVATE_WORKFLOW_COMMENT", json.dumps(result))
        self.assertEqual(await self.page.evaluate("window.approvals"), 0)
