"""Rules of the co-booking confirmation added after review. Offline only:
mocks, a temp SQLite file and a Chromium page behind a dead proxy."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError, async_playwright

from sagasu import fbs_tasks
from sagasu.action_journal import ActionJournal, action_summary
from sagasu.fbs_client import LayoutChanged
from sagasu.fbs_tasks import (
    DETAILS_PATH,
    TaskUnavailable,
    confirm_task,
    open_confirmation_page,
    verify_confirmed,
)
from sagasu.mcp_service import Job
from sagasu.reconciliation import reconcile

TASK = {
    "reference_number": "BK-20990101-000062",
    "date_time": "22-Nov-2099 10:30 - 12:30 (2 hrs)",
    "room": "LKSLIB Project Room 4-11",
    "request_type": "Booking Request",
    "requested_by": "ALEX TAN",
    "status": "Pending Confirmation",
    "can_approve": False,
    "can_view": True,
    "can_confirm": True,
}


def listing(*tasks, complete=True):
    return {
        "tasks": [dict(t) for t in tasks],
        "total": len(tasks),
        "complete": complete,
    }


def client():
    return NS(page=NS(wait_for_timeout=AsyncMock()), progress=AsyncMock())


class VerifyConfirmedTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self, *reads):
        with patch.object(
            fbs_tasks, "read_task_list", AsyncMock(side_effect=list(reads))
        ):
            return await verify_confirmed(client(), TASK)

    async def test_only_positive_statuses_or_a_vanished_reference_verify(self):
        for status in ("Confirmed", "Approved", "Completed"):
            with self.subTest(status=status):
                self.assertEqual(
                    await self.verify(listing({**TASK, "status": status})), status
                )
        self.assertEqual(await self.verify(listing()), "Confirmed (left the Task List)")

    async def test_rejected_or_cancelled_is_never_a_confirmation(self):
        for status in ("Rejected", "Cancelled", "Withdrawn", "Not Confirmed"):
            with self.subTest(status=status), self.assertRaises(LayoutChanged):
                await self.verify(*[listing({**TASK, "status": status})] * 4)

    async def test_a_same_reference_row_still_pending_is_not_gone(self):
        # Regression: a row whose date/time text changed no longer matched the
        # task's identity, so the task looked "gone" while still pending.
        moved = {**TASK, "date_time": "22-Nov-2099 10:30 - 12:30"}
        with self.assertRaises(LayoutChanged):
            await self.verify(*[listing(moved)] * 4)

    async def test_an_incomplete_listing_never_proves_it_left(self):
        with self.assertRaises(LayoutChanged):
            await self.verify(*[listing(complete=False)] * 4)


class PressNeverHappenedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_browser_error_before_the_press_submits_nothing(self):
        # Regression: it surfaced as outcome_unknown and blocked the reference.
        with patch.object(
            fbs_tasks,
            "locate_reviewed_task",
            AsyncMock(side_effect=PlaywrightError("Target closed")),
        ):
            with self.assertRaises(TaskUnavailable) as caught:
                await confirm_task(client(), TASK)
        self.assertIn("No confirmation was submitted", str(caught.exception))


PAGE = """<!doctype html><body>
<span id="panel_spanWorkflowButtons"><span id="panel_datalistWorkflowButtons">
  <a id="panel_datalistWorkflowButtons_ctl01_buttonWorkflowAction" href="#"><span>Reject</span></a>
  __MAIN__
</span></span>
<div id="panel_buttonShadow"><span id="panel_datalistWorkflowButtons">
  <a id="panel_datalistWorkflowButtons_ctl02_buttonWorkflowAction" href="#"><span>Confirm</span></a>
</span></div>
<div class="field full" id="GridViewBookingDetails_gv_ctl02_bookingFormControl1_ObjectNumber"><div class="table">
  <div class="caption side left"><span>Ref Number:</span></div>
  <div class="control"><span class="label">BK-20990101-000062</span></div>
</div></div></body>"""
MAIN_CONFIRM = '<a id="panel_datalistWorkflowButtons_ctl02_buttonWorkflowAction" href="#"><span>Confirm</span></a>'


class ShadowCopyTests(unittest.IsolatedAsyncioTestCase):
    async def page_with(self, main):
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"}
        )
        self.addAsyncCleanup(browser.close)
        page = await browser.new_page()
        base = "https://fbs.intranet.smu.edu.sg"

        async def route(request_route):
            url = request_route.request.url
            if DETAILS_PATH in url:
                body = PAGE.replace("__MAIN__", main)
            else:
                body = f'<iframe name="frameContent" src="{base}{DETAILS_PATH}?ID=x"></iframe>'
            await request_route.fulfill(body=body, content_type="text/html")

        await page.route("**/*", route)
        await page.goto(base + "/home")
        await page.frame(name="frameContent").wait_for_load_state()
        row = NS(locator=lambda selector: NS(click=AsyncMock()))
        return NS(page=page, progress=AsyncMock(), _wait_for_idle=AsyncMock()), row

    async def test_the_visible_shadow_copy_is_never_the_confirm_button(self):
        # The shadow copy here is NOT hidden by CSS: only its container rules it out.
        session, row = await self.page_with(MAIN_CONFIRM)
        frame, button = await open_confirmation_page(session, TASK, row)
        self.assertFalse(
            await button.evaluate("el => !!el.closest('#panel_buttonShadow')")
        )

    async def test_a_confirm_only_in_the_shadow_copy_is_refused(self):
        session, row = await self.page_with("")
        with patch.object(fbs_tasks, "CONFIRM_PAGE_SECONDS", 1.0):
            with self.assertRaises(TaskUnavailable):
                await open_confirmation_page(session, TASK, row)


class NotApprovedReconciliationTests(unittest.IsolatedAsyncioTestCase):
    SUMMARY = action_summary("approve_task", TASK)

    async def run_reconcile(self, rows, *, settled=True, complete=True):
        payload = {
            "kind": "approve_task",
            "summary": dict(self.SUMMARY),
            "reference_number": TASK["reference_number"],
            "settled": settled,
        }
        with patch(
            "sagasu.reconciliation.read_task_list",
            AsyncMock(return_value=listing(*rows, complete=complete)),
        ):
            return await reconcile(client(), payload)

    def test_the_reviewed_status_is_recorded(self):
        self.assertEqual(self.SUMMARY["status"], "Pending Confirmation")

    async def test_still_pending_and_settled_is_verified_not_approved(self):
        evidence = await self.run_reconcile([TASK])
        self.assertEqual(evidence["state"], "not_approved")
        self.assertTrue(evidence["action_outcome_verified"])
        self.assertTrue(evidence["safe_to_retry"])

    async def test_anything_else_is_not_a_safe_retry(self):
        cases = {
            "not settled": dict(rows=[TASK], settled=False),
            "incomplete": dict(rows=[TASK], complete=False),
            "status moved on": dict(rows=[{**TASK, "status": "Pending Approval"}]),
            "room changed": dict(rows=[{**TASK, "room": "Other"}]),
            "gone": dict(rows=[]),
            "twice": dict(rows=[TASK, TASK]),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                evidence = await self.run_reconcile(case.pop("rows"), **case)
                self.assertNotEqual(evidence["state"], "not_approved")
                self.assertFalse(evidence["safe_to_retry"])

    async def test_a_confirmed_task_verifies_as_approved(self):
        evidence = await self.run_reconcile([{**TASK, "status": "Confirmed"}])
        self.assertEqual(evidence["state"], "approved")
        self.assertTrue(evidence["action_outcome_verified"])


class NotApprovedJournalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.journal = ActionJournal(Path(folder.name) / "actions.sqlite3")

    def unknown(self, kind, operation_id):
        job = Job(operation_id, kind, 1.0, updated_at=1.0)
        summary = (
            action_summary("approve_task", TASK)
            if kind == "approve_task"
            else {
                "reference_number": TASK["reference_number"],
                "room": TASK["room"],
                "date_time": TASK["date_time"],
                "fbs_status": "Confirmed",
            }
        )
        self.journal.reserve(job, "review-" + operation_id, summary)
        job.status = "outcome_unknown"
        self.journal.update(job)

    def record(self, operation_id, state):
        return self.journal.record_reconciliation(
            operation_id,
            {"state": state, "action_outcome_verified": True, "safe_to_retry": True},
            2.0,
        )

    def test_each_did_not_happen_state_counts_only_for_its_own_kind(self):
        self.unknown("approve_task", "op-approve")
        self.unknown("cancel", "op-cancel")
        stored = self.record("op-approve", "not_approved")
        self.assertTrue(stored["action_outcome_verified"] and stored["safe_to_retry"])
        for operation_id, state in (
            ("op-cancel", "not_approved"),
            ("op-approve", "not_cancelled"),
        ):
            with self.subTest(operation_id=operation_id, state=state):
                stored = self.record(operation_id, state)
                self.assertFalse(stored["action_outcome_verified"])
                self.assertFalse(stored["safe_to_retry"])
