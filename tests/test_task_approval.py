"""Task approval against browser fixtures; no live FBS requests or mutations."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from sagasu.fbs_account import TASK_SNAPSHOT, parse_tasks
from sagasu.fbs_client import LayoutChanged
from sagasu.fbs_tasks import (
    TaskUnavailable,
    TaskApprovalOutcomeUnknown,
    add_task_capabilities,
    approval_acknowledged,
    approve_task,
    capture_task_details,
    execute_task_action,
)

TASK = {
    "reference_number": "BK-TEST",
    "date_time": "03-Nov-2026 14:00 - 16:00",
    "room": "Room A",
    "request_type": "Booking Approval",
    "requested_by": "A student",
    "status": "Pending Approval",
    "can_approve": True,
    "can_view": True,
}

FIXTURE = """<!doctype html><body>
<div id="MessagePanel" style="display:none"><a href="#" onclick="document.querySelector('#MessagePanel').style.display='none';return false">Dismiss</a><span id="MessageLabel"></span></div>
<div id="GridViewTask">
<a id="GridViewTask_ctl13" href="#" onclick="approve();return false">&nbsp;Approve Selected</a>
<a href="#" onclick="window.rejectCount++;return false">Reject Selected</a>
<table><thead><tr><th><input type="checkbox" id="all" onclick="document.querySelectorAll('tr.row input').forEach(e=>e.checked=this.checked)"></th><th>REF. NUMBER</th><th>DATE &amp; TIME OF USE</th><th>BUILDING</th><th>FACILITY</th><th>TYPE OF REQUEST</th><th>REQUESTED BY</th><th>STATUS</th><th></th></tr></thead>
<tbody id="GridViewTask_content">
<tr class="row"><td><input type="checkbox" id="other" checked></td><td>BK-OTHER</td><td>03-Nov-2026 14:00 - 16:00</td><td>SCIS1</td><td>Room B</td><td>Booking Approval</td><td>B student</td><td>Pending Approval</td><td><a id="other_BtnView">VIEW</a></td></tr>
<tr class="row"><td><input type="checkbox" id="target" onchange="if(window.mode==='select_many')document.querySelector('#other').checked=true"></td><td>BK-TEST</td><td>03-Nov-2026 14:00 - 16:00</td><td>SCIS1</td><td>Room A</td><td>Booking Approval</td><td>A student</td><td>Pending Approval</td><td><a id="target_BtnView" href="#" onclick="details();return false">VIEW</a></td></tr>
</tbody></table><span id="total">2 ITEM(S) TOTAL</span></div>
<script>
window.approvalCount=0; window.rejectCount=0; window.selected=[]; window.mode='success';
function approve(){
  window.approvalCount++;
  window.selected=[...document.querySelectorAll('tr.row input:checked')].map(e=>e.id);
  if(window.mode==='native_confirm' && !confirm('Approve this booking task?')) return;
  if(window.mode==='wrong_dialog' && !confirm('Delete this booking?')) return;
  if(window.mode==='stay') return;
  if(window.mode==='status'){document.querySelector('#target').closest('tr').cells[7].innerText='Approved';return;}
  document.querySelector('#target').closest('tr').remove();
  document.querySelector('#total').innerText='1 ITEM(S) TOTAL';
  if(window.mode==='disappear') return;
  document.querySelector('#MessageLabel').innerText=window.mode==='wrong_reference'?'BK-OTHER approved successfully.':'1 task approved successfully.';
  document.querySelector('#MessagePanel').style.display='block';
}
function details(){
  document.body.innerHTML='<h1>Booking details</h1><div id="bookingFormControl1_ObjectNumber"><span>'+ (window.mode==='wrong_details'?'BK-OTHER':'BK-TEST') +'</span></div>';
}
</script></body>"""


class AcknowledgementTests(unittest.TestCase):
    def test_requires_positive_approval_message(self):
        for message in (
            "1 task approved successfully.",
            "Tasks have been approved.",
            "Booking confirmed successfully.",
        ):
            self.assertTrue(approval_acknowledged(message, "BK-TEST"), message)
        for message in (
            "Unable to approve tasks",
            "Tasks not approved successfully",
            "0 tasks approved successfully",
            "2 tasks approved successfully",
            "BK-OTHER approved successfully",
            "Task removed",
            "Pending Approval",
        ):
            self.assertFalse(approval_acknowledged(message, "BK-TEST"), message)


class BrowserApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.set_content('<iframe name="frameContent"></iframe>')
        self.frame = self.page.frame(name="frameContent")
        await self.frame.set_content(FIXTURE)
        proxy = NS(
            frame=self.page.frame,
            on=self.page.on,
            remove_listener=self.page.remove_listener,
            frames=self.page.frames,
            wait_for_timeout=AsyncMock(),
        )
        self.client = NS(page=proxy, progress=AsyncMock(), _wait_for_idle=AsyncMock())

        async def read(client):
            result = parse_tasks(await self.frame.evaluate(TASK_SNAPSHOT))
            await add_task_capabilities(self.frame, result)
            return result

        self.read = read
        patched = patch("sagasu.fbs_tasks.read_task_list", read)
        patched.start()
        self.addCleanup(patched.stop)
        receipt = patch(
            "sagasu.fbs_tasks.capture_booking_evidence",
            AsyncMock(return_value=b"booking-details-png"),
        )
        self.details_receipt = receipt.start()
        self.addCleanup(receipt.stop)

    async def mode(self, name):
        await self.frame.evaluate("value=>window.mode=value", name)

    async def test_capabilities_follow_actual_controls_and_pending_status(self):
        result = await self.read(self.client)
        self.assertTrue(result["tasks"][1]["can_approve"])
        await self.frame.locator("#target").evaluate("e=>e.disabled=true")
        self.assertFalse((await self.read(self.client))["tasks"][1]["can_approve"])

    async def test_approves_only_reviewed_task_once_and_captures_details(self):
        result = await approve_task(self.client, TASK)
        self.assertEqual(result["status"], "approved")
        self.assertFalse(result["booking_status_verified"])
        self.assertEqual(result["receipt_kind"], "booking_details")
        self.assertEqual(await self.frame.evaluate("window.selected"), ["target"])
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 1)
        self.assertEqual(await self.frame.evaluate("window.rejectCount"), 0)

    async def test_approved_task_status_is_positive_evidence(self):
        await self.mode("status")
        result = await approve_task(self.client, TASK)
        self.assertEqual(result["task_status"], "Approved")

    async def test_verified_approval_is_checkpointed_before_receipt_capture(self):
        with patch("sagasu.fbs_tasks.checkpoint_verified") as checkpoint:

            async def capture(client, frame, outcome):
                checkpoint.assert_called_once()
                self.assertEqual(
                    checkpoint.call_args.args[0]["reference_number"],
                    TASK["reference_number"],
                )

            with patch(
                "sagasu.fbs_tasks.capture_approval_receipt", side_effect=capture
            ):
                result = await approve_task(self.client, TASK)
        self.assertEqual(result["status"], "approved")

    async def test_selection_of_multiple_tasks_is_rejected_before_approval(self):
        await self.mode("select_many")
        with self.assertRaises(TaskUnavailable):
            await approve_task(self.client, TASK)
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 0)

    async def test_changed_request_or_status_fails_before_approval(self):
        for changes in (
            {"room": "Different room"},
            {"requested_by": "Other student"},
            {"request_type": "Cancellation"},
            {"status": "Confirmed"},
        ):
            with self.assertRaises(TaskUnavailable):
                await approve_task(self.client, {**TASK, **changes})
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 0)

    async def test_duplicate_task_identity_fails_before_approval(self):
        await self.frame.locator("#target").evaluate(
            "e=>e.closest('tbody').append(e.closest('tr').cloneNode(true))"
        )
        await self.frame.locator("#total").evaluate("e=>e.innerText='3 ITEM(S) TOTAL'")
        with self.assertRaises(TaskUnavailable):
            await approve_task(self.client, TASK)
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 0)

    async def test_disappearing_row_without_acknowledgement_is_unknown(self):
        await self.mode("disappear")
        with self.assertRaises(TaskApprovalOutcomeUnknown):
            await approve_task(self.client, TASK)
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 1)
        self.details_receipt.assert_not_awaited()

    async def test_acknowledgement_for_another_reference_is_unknown(self):
        await self.mode("wrong_reference")
        with self.assertRaises(TaskApprovalOutcomeUnknown):
            await approve_task(self.client, TASK)
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 1)

    async def test_stale_success_notice_is_cleared(self):
        await self.mode("disappear")
        await self.frame.locator("#MessageLabel").evaluate(
            "e=>e.innerText='1 task approved successfully.'"
        )
        await self.frame.locator("#MessagePanel").evaluate("e=>e.style.display='block'")
        with self.assertRaises(TaskApprovalOutcomeUnknown):
            await approve_task(self.client, TASK)

    async def test_native_approval_confirmation_is_handled(self):
        await self.mode("native_confirm")
        self.assertEqual((await approve_task(self.client, TASK))["status"], "approved")

    async def test_unrelated_native_dialog_is_dismissed(self):
        await self.mode("wrong_dialog")
        with self.assertRaises(TaskApprovalOutcomeUnknown):
            await approve_task(self.client, TASK)
        self.assertEqual(await self.frame.locator("#target").count(), 1)

    async def test_details_failure_falls_back_to_acknowledgement_png(self):
        self.details_receipt.side_effect = LayoutChanged("Not in My Bookings")
        result = await approve_task(self.client, TASK)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["receipt_kind"], "approval_acknowledgement")
        self.assertTrue(result["receipt_png"].startswith(b"\x89PNG"))

    async def test_readonly_details_capture_does_not_approve(self):
        result = await capture_task_details(self.client, TASK)
        self.assertEqual(result["status"], "details")
        self.assertTrue(result["receipt_png"].startswith(b"\x89PNG"))
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 0)

    async def test_readonly_details_reject_wrong_reference(self):
        await self.mode("wrong_details")
        with self.assertRaises(LayoutChanged):
            await capture_task_details(self.client, TASK)
        self.assertEqual(await self.frame.evaluate("window.approvalCount"), 0)


class TaskWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_is_default_and_submission_is_explicit(self):
        with (
            patch("sagasu.fbs_tasks.FBSClient"),
            patch(
                "sagasu.fbs_tasks.capture_task_details",
                AsyncMock(return_value={"status": "details"}),
            ) as details,
            patch(
                "sagasu.fbs_tasks.approve_task",
                AsyncMock(return_value={"status": "approved"}),
            ) as approve,
        ):
            await execute_task_action(TASK)
            details.assert_awaited_once()
            approve.assert_not_awaited()
            await execute_task_action(TASK, approve=True)
            approve.assert_awaited_once()
