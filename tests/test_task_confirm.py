"""A co-booker confirms one task on its own FBS page; a stand-in, never live FBS.

The stand-in is shaped like the pages FBS served on 21 Nov 2026 (sanitised):

- Task List (modules/homenavigation/tasklist.aspx) arrives as a hidden form1 that
  script reveals. "Approve Selected" / "Reject Selected" sit in a hidden toolbar
  (GridViewTask_actsonitems) that appears only once a row is ticked, so a
  co-booker's row is not approvable from the list. Its View is a postback.
- The task's page (modules/homenavigation/bookingenquirytask.aspx?ID=...&TYPE=...)
  shows the Ref Number and FBS's workflow buttons, Reject and Confirm, with a
  CSS-hidden duplicate under #panel_buttonShadow. Confirm posts the page back to
  itself with no confirm() prompt. What FBS shows afterwards is not known; here
  it takes the task off the Task List and returns there. A separate My Bookings
  read supplies the positive evidence needed when the task disappears.

The contract held here: a refusal before Confirm is pressed is TaskUnavailable
and FBS receives no POST; after the one press, anything short of Task List
evidence is TaskApprovalOutcomeUnknown, never a second press.

Chromium runs behind a dead proxy and every request is routed, so nothing
leaves this machine. Redirects are HTML location.replace() pages, never 3xx.
"""

import contextlib
import copy
import html
import json
import re
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from sagasu import fbs_tasks
from sagasu.fbs_account import TASK_SNAPSHOT, parse_tasks
from sagasu.fbs_client import FBS_URL, FBSClient, LayoutChanged
from sagasu.fbs_http import parse_account_document
from sagasu.fbs_tasks import (
    DETAILS_PATH,
    IDENTITY,
    TaskApprovalOutcomeUnknown,
    TaskUnavailable,
    add_task_capabilities,
    confirm_task,
    confirmable,
    execute_task_action,
    locate_reviewed_task,
    open_confirmation_page,
)
from sagasu.mcp_service import AgentError, AgentService, Backend
from tests.test_mcp import TASK as APPROVER_TASK
from tests.test_mcp import ServiceCase
from tests.test_remote import remote_origin

try:
    from mcp import Client
    from sagasu.mcp_server import RemoteAccess, create_server
except ImportError:
    Client = None

BASE = "https://fbs.intranet.smu.edu.sg"
LIST_PATH = "/modules/homenavigation/tasklist.aspx"
LIST_URL = BASE + LIST_PATH
PNG = b"\x89PNG\r\n\x1a\nstand-in-booking-details"
ZERO_PENALTY = (
    "A Cancellation Penalty of [0.00] will be imposed. "
    "Please click Ok if you wish to proceed"
)

# The one row FBS listed on 21 Nov 2026, sanitised.
ROW = {
    "reference_number": "BK-20990101-000062",
    "date_time": "22-Nov-2099 10:30 - 12:30 (2 hrs)",
    "building": "Li Ka Shing Library",
    "room": "LKSLIB Project Room 4-11",
    "request_type": "Booking Request",
    "requested_by": "ALEX TAN",
    "status": "Pending Confirmation",
}
# What a browser read of that Task List reports (and a review carries).
TASK = {
    **{key: ROW[key] for key in (*IDENTITY, "status")},
    "can_approve": False,
    "can_view": True,
    "can_confirm": True,
}
KEY = "62"  # the stand-in's ID for ROW's task page
CONFIRMED = [("Confirm", KEY)]

SHELL = """<script>
var btn = {cl: function () {}, kp: function () { return true; }};
function v() {}
function __doPostBack(target, argument) {
  var form = document.forms[0];
  form.__EVENTTARGET.value = target;
  form.__EVENTARGUMENT.value = argument;
  form.submit();
}
var gridv = {initgv: function (id) {
  var grid = document.getElementById(id), bar = document.getElementById(id + '_actsonitems');
  grid.addEventListener('change', function () {
    var ticked = grid.querySelector('#' + id + '_content input[type=checkbox]:checked');
    bar.style.display = ticked ? '' : 'none';
  });
}};
</script>"""

# FBS's shell serves form1 hidden; its startup script reveals it.
REVEAL = """<script>
document.addEventListener('DOMContentLoaded', function () {
  %s
  setTimeout(function () { document.getElementById('form1').style.display = ''; }, 30);
});
</script>"""

HEADERS = [
    "",
    "REF. NUMBER",
    "DATE &amp; TIME OF USE",
    "BUILDING",
    "FACILITY",
    "TYPE OF REQUEST",
    "REQUESTED BY",
    "STATUS",
    "",
]


def postback(target):
    """FBS's link postback, as served: the click returns, a timer posts the form."""
    return (
        "btn.cl(this);setTimeout('__doPostBack(\\'"
        + target
        + "\\',\\'\\')', 0);return false;"
    )


def fbs_page(action, body, startup=""):
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>SMU FBS</title>'
        + SHELL
        + '</head><body><div id="__divPage__">'
        + f'<form method="post" action="{html.escape(action)}" id="form1" '
        + 'style="display: none; height:100%;">'
        + '<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />'
        + '<input type="hidden" name="__EVENTARGUMENT" id="__EVENTARGUMENT" value="" />'
        + '<input type="hidden" name="__ViewStateKey__" id="__ViewStateKey__" value="VSK-STANDIN" />'
        + body
        + "</form></div>"
        + REVEAL % startup
        + "</body></html>"
    )


def task_row(ctl, task):
    view = postback(f"GridViewTask$gv${ctl}$BtnView$BtnView_bt")
    cells = [
        f'<input id="GridViewTask_gv_{ctl}_checkMultiple" type="checkbox" '
        f'name="GridViewTask$gv${ctl}$checkMultiple" />',
        *(
            html.escape(task[key])
            for key in (
                "reference_number",
                "date_time",
                "building",
                "room",
                "request_type",
                "requested_by",
                "status",
            )
        ),
        f'<a id="GridViewTask_gv_{ctl}_BtnView" onclick="{view}" href="javascript:v();">View</a>',
    ]
    return '<tr class="row">' + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def task_list(tasks, approve_link=True):
    """The Task List page for these tasks: (html, {row control: task key}).

    The real page carries Approve Selected in the tick-to-show toolbar;
    approve_link=False drops it, so a task can only be confirmed on its page."""
    rendered, rows = {}, []
    for index, task in enumerate(tasks):
        ctl = f"ctl{index + 2:02d}"
        rendered[ctl] = task["key"]
        rows.append(task_row(ctl, task))
    if not rows:
        rows.append('<tr class="row"><td colspan="9">No items</td></tr>')
    toolbar = (
        '<span id="GridViewTask_actsonitems" class="actsonitems" style="display:none;">'
        + (
            f'<a id="GridViewTask_ctl13" onclick="{postback("GridViewTask$ctl13$ctl00")}" '
            + 'onkeypress="return btn.kp();" href="javascript:v();" class="btnblue">'
            + "<span><img align='absmiddle' src='/images/tick2.png' />&nbsp;Approve Selected</span></a>"
            if approve_link
            else ""
        )
        + f'<a id="GridViewTask_ctl14" onclick="{postback("GridViewTask$ctl14$ctl00")}" '
        + 'onkeypress="return btn.kp();" href="javascript:v();" class="btnblue">'
        + "<span><img align='absmiddle' src='/images/cross2.png' />&nbsp;Reject Selected</span></a>"
        + "</span>"
    )
    body = (
        '<div class="search"><input name="txtSearch_c1" type="text" id="txtSearch_c1" value="" />'
        '<a id="buttonReset" href="javascript:v();">Reset</a></div>'
        '<div id="GridViewTask">'
        + toolbar
        + '<table class="grid"><thead><tr>'
        + "".join(f"<th>{label}</th>" for label in HEADERS)
        + '</tr></thead><tbody id="GridViewTask_content">'
        + "".join(rows)
        + "</tbody></table>"
        + f'<div class="pager"><span>{len(tasks)} ITEM(S) TOTAL</span></div></div>'
    )
    return fbs_page(LIST_URL, body, 'gridv.initgv("GridViewTask", {});'), rendered


def workflow_target(n):
    return f"panel$datalistWorkflowButtons$ctl{n:02d}$buttonWorkflowAction$buttonWorkflowAction_bt"


def workflow_buttons(labels, prompt=None):
    links = []
    for n, label in enumerate(labels, start=1):
        guard = (
            f"if(!confirm('{prompt}'))return false;"
            if prompt and label == "Confirm"
            else ""
        )
        links.append(
            f'<a id="panel_datalistWorkflowButtons_ctl{n:02d}_buttonWorkflowAction" '
            f'onclick="{guard}{postback(workflow_target(n))}" onkeypress="return btn.kp();" '
            f'href="javascript:v();" class="btnblue"><span>{html.escape(label)}</span></a>'
        )
    return "".join(links)


def detail_field(name, caption, value):
    return (
        f'<div class="field full" id="GridViewBookingDetails_gv_ctl02_bookingFormControl1_{name}">'
        '<div class="table"><div class="caption side left">'
        f"<span>{html.escape(caption)}</span></div>"
        f'<div class="control"><span class="label">{html.escape(value)}</span></div>'
        "</div></div>"
    )


class PageProxy:
    """The real page, with FBS-length pauses cut short (and recorded)."""

    def __init__(self, page):
        self._page = page
        self.waits = []

    def __getattr__(self, name):
        return getattr(self._page, name)

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)
        await self._page.wait_for_timeout(min(ms, 50))


class ConfirmStandIn:
    """FBS's Task List and task page, routed in a real Chromium.

    Knobs: labels (the visible workflow buttons, in order); shadow_labels (the
    hidden duplicate under #panel_buttonShadow; None: same as labels);
    page_reference (the Ref Number the task's page shows; None: the task's own);
    confirm_prompt (a confirm() guard on Confirm's onclick; None, as served);
    open_page (whether View leads to the task's page); buttons_ms (delay before
    the workflow buttons appear); details_base (the task page's URL, for case);
    drop_post (abort the Confirm POST); after_confirm ("remove": the task leaves
    the Task List; "pending": unchanged; "duplicate": listed twice; else its new
    status); incomplete_after_post (Task List reads after a POST say incomplete).

    Records: posts [(label, key)] for every workflow-button POST; views [key];
    bulk (Approve/Reject Selected posts); dialogs; unexpected requests; reads.
    """

    async def setup(self, case):
        self.tasks = [dict(ROW, key=KEY)]
        self.labels = ["Reject", "Confirm"]
        self.shadow_labels = None
        self.page_reference = None
        self.confirm_prompt = None
        self.open_page = True
        self.buttons_ms = 0
        self.details_base = BASE + DETAILS_PATH
        self.drop_post = False
        self.after_confirm = "remove"
        self.bookings = [
            {
                "reference_number": ROW["reference_number"],
                "room": ROW["room"],
                "date_time": ROW["date_time"],
                "fbs_status": "Pending Confirmation",
            }
        ]
        self.incomplete_after_post = False
        # The confirm route is for tasks without Approve Selected; the
        # capability tests switch the real page's button back on.
        self.approve_link = False
        self.posts, self.views, self.bulk, self.dialogs = [], [], [], []
        self.unexpected, self.listings, self.contexts = [], [], []
        self.reads = 0
        self.rendered = {}
        self.redirecting = False
        driver = await async_playwright().start()
        case.addAsyncCleanup(driver.stop)
        # A dead proxy: a request the routes below do not answer fails here
        # instead of reaching FBS or Microsoft.
        self.browser = await driver.chromium.launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"}
        )
        case.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.page.on("dialog", self.on_dialog)
        await self.page.route("**/*", self.route)
        await self.page.goto(FBS_URL)
        await self.frame().locator("#GridViewTask_content").wait_for(state="visible")
        self.proxy = PageProxy(self.page)
        self.client = NS(
            page=self.proxy,
            progress=AsyncMock(),
            _wait_for_idle=FBSClient(progress=AsyncMock())._wait_for_idle,
        )
        self.evidence = AsyncMock(return_value=PNG)
        for target, value in (
            ("sagasu.fbs_tasks.read_task_list", self.read),
            ("sagasu.fbs_account.read_bookings", self.read_bookings),
            ("sagasu.fbs_tasks.capture_booking_evidence", self.evidence),
            ("sagasu.fbs_tasks.CONFIRM_PAGE_SECONDS", 10),
            ("sagasu.fbs_tasks.CONFIRM_SUBMIT_MS", 10000),
            ("sagasu.fbs_tasks.CONFIRM_CHECKS", 3),
        ):
            patched = patch(target, value)
            patched.start()
            case.addCleanup(patched.stop)

    def frame(self):
        return self.page.frame(name="frameContent")

    def task(self, key):
        return next((task for task in self.tasks if task["key"] == key), None)

    @staticmethod
    def key_of(url):
        return (parse_qs(urlsplit(url).query).get("ID") or [""])[0].lstrip("#")

    def details_url(self, key):
        return f"{self.details_base}?ID=%23{key}&TYPE=%23BookingRequest"

    @asynccontextmanager
    async def context(self, **kwargs):
        """Stands in for FBSClient(...) in execute_task_action."""
        self.contexts.append(kwargs)
        yield self.client

    async def on_dialog(self, dialog):
        self.dialogs.append(dialog.message)
        await dialog.dismiss()

    async def read(self, client, *, reference=None):
        """Stands in for read_task_list: a fresh load of the stand-in Task List."""
        self.reads += 1
        frame = self.frame()
        if self.redirecting:  # let a redirect already under way land first
            with contextlib.suppress(PlaywrightError):
                await frame.wait_for_url(
                    lambda url: urlsplit(url).path.casefold() == LIST_PATH,
                    timeout=5000,
                )
        for attempt in range(5):
            try:
                await frame.goto(LIST_URL)
                break
            except PlaywrightError:
                if attempt == 4:
                    raise
        await frame.locator("#GridViewTask_content").wait_for(
            state="visible", timeout=10000
        )
        result = parse_tasks(await frame.evaluate(TASK_SNAPSHOT))
        await add_task_capabilities(frame, result)
        if self.incomplete_after_post and self.posts:
            result["complete"] = False
        self.listings.append(copy.deepcopy(result))
        return result

    def listing(self):
        body, self.rendered = task_list(self.tasks, self.approve_link)
        return body

    async def read_bookings(self, client, *, reference=None):
        rows = copy.deepcopy(self.bookings if self.posts else [])
        return {"bookings": rows, "total": len(rows), "complete": True}

    def task_page(self, task):
        buttons = workflow_buttons(self.labels, self.confirm_prompt)
        shadow = workflow_buttons(
            self.labels if self.shadow_labels is None else self.shadow_labels,
            self.confirm_prompt,
        )
        late = ""
        if self.buttons_ms:
            late = (
                "setTimeout(function () { document.getElementById("
                f"'panel_datalistWorkflowButtons').innerHTML = {json.dumps(buttons)}; "
                f"}}, {self.buttons_ms});"
            )
            buttons = ""
        reference = self.page_reference or task["reference_number"]
        body = (
            "<style>#panel_buttonShadow { display: none; }</style>"
            '<div id="panel_ButtonsPanel"><span id="panel_spanWorkflowButtons">'
            f'<span id="panel_datalistWorkflowButtons">{buttons}</span>'
            '<a id="panel_buttonReturn" onclick="return false;" href="javascript:v();" '
            'class="btn"><span>Back</span></a></span></div>'
            '<div id="panel_buttonShadow"><span id="panel_datalistWorkflowButtons">'
            f"{shadow}</span></div>"
            '<div id="GridViewBookingDetails">'
            + detail_field("ObjectNumber", "Ref Number:", reference)
            + detail_field("Facility", "Facility:", task["room"])
            + detail_field("BookingDateTime", "Date & Time:", task["date_time"])
            + "</div>"
        )
        return fbs_page(self.details_url(task["key"]), body, late)

    def confirmed(self, key):
        task = self.task(key)
        if self.after_confirm == "remove":
            self.tasks.remove(task)
        elif self.after_confirm == "duplicate":
            self.tasks.append({**task, "key": key + "b"})
        elif self.after_confirm != "pending":
            task["status"] = self.after_confirm

    @staticmethod
    def redirect(url):
        # Not a 3xx: Playwright does not route the request a fulfilled
        # redirect leads to, so it would leave the stand-in.
        return f"<script>location.replace({json.dumps(url)})</script>"

    async def route(self, route):
        request = route.request
        url = request.url
        path = urlsplit(url).path.casefold()
        if request.method == "POST":
            fields = parse_qs(request.post_data or "", keep_blank_values=True)
            target = (fields.get("__EVENTTARGET") or [""])[0]
            if path == LIST_PATH:
                view = re.fullmatch(
                    r"GridViewTask\$gv\$(ctl\d\d)\$BtnView\$BtnView_bt", target
                )
                if view and view[1] in self.rendered:
                    key = self.rendered[view[1]]
                    self.views.append(key)
                    body = (
                        self.redirect(self.details_url(key))
                        if self.open_page and self.task(key)
                        else self.listing()
                    )
                    await route.fulfill(body=body, content_type="text/html")
                    return
                if target in {"GridViewTask$ctl13$ctl00", "GridViewTask$ctl14$ctl00"}:
                    self.bulk.append(target)
                    await route.fulfill(body=self.listing(), content_type="text/html")
                    return
            if path == DETAILS_PATH:
                button = re.fullmatch(
                    r"panel\$datalistWorkflowButtons\$ctl(\d\d)\$buttonWorkflowAction"
                    r"\$buttonWorkflowAction_bt",
                    target,
                )
                key = self.key_of(url)
                if button and self.task(key):
                    index = int(button[1]) - 1
                    label = self.labels[index] if 0 <= index < len(self.labels) else "?"
                    self.posts.append((label, key))
                    if self.drop_post:
                        await route.abort()
                        return
                    if label == "Confirm":
                        self.confirmed(key)
                    self.redirecting = True
                    await route.fulfill(
                        body=self.redirect(LIST_URL), content_type="text/html"
                    )
                    return
            self.unexpected.append((request.method, path, target))
            await route.fulfill(status=400, body="unexpected post")
            return
        if url == FBS_URL:
            body = (
                f'<!doctype html><html><body><iframe name="frameContent" src="{LIST_URL}" '
                'style="width:1200px;height:800px"></iframe></body></html>'
            )
        elif path == LIST_PATH:
            self.redirecting = False
            body = self.listing()
        elif path == DETAILS_PATH and self.task(self.key_of(url)):
            body = self.task_page(self.task(self.key_of(url)))
        else:
            if not path.endswith((".png", ".ico")):
                self.unexpected.append((request.method, path, ""))
            await route.fulfill(status=404, body="")
            return
        await route.fulfill(body=body, content_type="text/html")


class StandInCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = ConfirmStandIn()
        await self.s.setup(self)

    def progress(self):
        return [call.args[0] for call in self.s.client.progress.await_args_list]

    def assert_unsubmitted(self):
        self.assertEqual(self.s.posts, [])
        self.assertEqual(self.s.bulk, [])
        self.assertEqual(self.s.dialogs, [])
        self.assertNotIn("submitting the confirmed co-booking", self.progress())
        self.s.evidence.assert_not_awaited()
        self.assertEqual(self.s.unexpected, [])

    async def refused(self, expected=TASK):
        with self.assertRaises(TaskUnavailable) as caught:
            await confirm_task(self.s.client, expected)
        return str(caught.exception)

    async def unknown(self):
        with self.assertRaises(TaskApprovalOutcomeUnknown) as caught:
            await confirm_task(self.s.client, TASK)
        self.assertEqual(self.s.posts, CONFIRMED)  # exactly one press
        self.assertEqual(self.s.bulk, [])
        self.s.evidence.assert_not_awaited()
        return str(caught.exception)


class ConfirmableTests(unittest.TestCase):
    def test_needs_a_view_and_exactly_pending_confirmation(self):
        self.assertTrue(
            confirmable({"can_view": True, "status": "Pending Confirmation"})
        )
        self.assertTrue(
            confirmable({"can_view": True, "status": " pending\n CONFIRMATION "})
        )
        for task in (
            {"can_view": False, "status": "Pending Confirmation"},
            {"can_view": "true", "status": "Pending Confirmation"},
            {"status": "Pending Confirmation"},
            {"can_view": True, "status": "Pending Approval"},
            {"can_view": True, "status": "Confirmed"},
            {"can_view": True, "status": "Pending Confirmation (Withdrawn)"},
            {"can_view": True},
        ):
            with self.subTest(task=task):
                self.assertFalse(confirmable(task))


class HTTPCapabilityTests(unittest.TestCase):
    """parse_account_document('tasks', ...) on the real-shaped Task List."""

    def test_co_booking_row_offers_approve_and_confirm(self):
        # FBS hides Approve Selected until a row is ticked (gridv.acton) and
        # serves form1 hidden until its script runs: neither hides the button.
        page, _ = task_list([dict(ROW, key=KEY)])
        result = parse_account_document("tasks", page)
        self.assertEqual((result["total"], result["complete"]), (1, True))
        (task,) = result["tasks"]
        self.assertEqual(task, {**TASK, "can_approve": True})

    def test_without_approve_selected_only_confirm_is_offered(self):
        page, _ = task_list([dict(ROW, key=KEY)], approve_link=False)
        (task,) = parse_account_document("tasks", page)["tasks"]
        self.assertEqual(task, TASK)

    def test_confirm_needs_pending_confirmation_and_a_visible_view(self):
        page, _ = task_list([dict(ROW, key=KEY, status="Pending Approval")])
        (task,) = parse_account_document("tasks", page)["tasks"]
        self.assertFalse(task["can_confirm"])
        page, _ = task_list([dict(ROW, key=KEY)])
        hidden = page.replace(
            'id="GridViewTask_gv_ctl02_BtnView"',
            'id="GridViewTask_gv_ctl02_BtnView" style="display:none"',
        )
        (task,) = parse_account_document("tasks", hidden)["tasks"]
        self.assertFalse(task["can_view"])
        self.assertFalse(task["can_confirm"])


class ConfirmReviewTests(ServiceCase):
    """review_task_approval / submit with the fake backend (tests/test_mcp.py)."""

    async def tasks(self, *rows):
        self.backend.tasks = [copy.deepcopy(row) for row in rows]
        return await self.finish(self.service.start("tasks"))

    async def test_confirm_only_task_is_reviewable_and_worded_as_confirm(self):
        job = await self.tasks(TASK)
        review = self.service.review_task_approval(job.id, TASK["reference_number"])
        self.assertEqual(review["kind"], "approve_task")
        self.assertEqual(review["summary"]["task"], TASK)
        action = review["summary"]["action"]
        self.assertTrue(action.startswith("Confirm this co-booking only"), action)
        self.assertNotIn("Approve", action)

    async def test_an_approvers_task_is_still_worded_as_approval(self):
        for row in (APPROVER_TASK, {**TASK, "can_approve": True}):
            with self.subTest(row=row):
                job = await self.tasks(row)
                review = self.service.review_task_approval(
                    job.id, row["reference_number"]
                )
                self.assertTrue(
                    review["summary"]["action"].startswith("Approve this task only")
                )

    async def test_a_task_offering_neither_action_is_refused(self):
        without = {key: value for key, value in TASK.items() if key != "can_confirm"}
        for row in ({**TASK, "can_confirm": False}, without):
            with self.subTest(row=row):
                job = await self.tasks(row)
                with self.assertRaises(AgentError):
                    self.service.review_task_approval(job.id, row["reference_number"])

    async def test_remote_caller_confirms_one_task_once_and_never_replays(self):
        with remote_origin():
            job = await self.tasks(TASK)
            review = self.service.review_task_approval(job.id, TASK["reference_number"])
            done = await self.finish(self.service.submit(review["review_id"]))
            self.assertEqual(done.status, "succeeded")
            self.assertEqual(done.result["status"], "approved")
            again = self.service.submit(review["review_id"])
            self.assertEqual(again["operation_id"], done.id)
            with self.assertRaises(AgentError):
                self.service.confirm(review["review_id"])
        submitted = [p for kind, p in self.backend.calls if kind == "approve_task"]
        self.assertEqual(submitted, [TASK])


class BrowserCapabilityTests(StandInCase):
    async def test_real_shaped_task_list_offers_approve_and_confirm(self):
        self.s.approve_link = True
        result = await self.s.read(self.s.client)
        self.assertEqual((result["total"], result["complete"]), (1, True))
        self.assertEqual(result["tasks"], [{**TASK, "can_approve": True}])

    async def test_without_approve_selected_only_confirm_is_offered(self):
        result = await self.s.read(self.s.client)
        self.assertEqual(result["tasks"], [TASK])

    async def test_ticking_the_row_reveals_approve_selected(self):
        # The toolbar is hidden until a row is ticked; either way it counts.
        self.s.approve_link = True
        await self.s.read(self.s.client)
        frame = self.s.frame()
        await frame.locator("#GridViewTask_gv_ctl02_checkMultiple").check()
        result = parse_tasks(await frame.evaluate(TASK_SNAPSHOT))
        await add_task_capabilities(frame, result)
        self.assertTrue(result["tasks"][0]["can_approve"])
        self.assertTrue(result["tasks"][0]["can_confirm"])
        self.assertEqual(self.s.bulk, [])


class BrowserConfirmTests(StandInCase):
    async def test_confirms_the_exact_task_once_and_reports_approved(self):
        result = await confirm_task(self.s.client, TASK)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["reference_number"], TASK["reference_number"])
        self.assertEqual(result["task"], TASK)
        self.assertEqual(result["task_status"], "Confirmed (left the Task List)")
        self.assertFalse(result["booking_status_verified"])
        self.assertEqual(
            (result["receipt_kind"], result["receipt_png"]), ("booking_details", PNG)
        )
        self.s.evidence.assert_awaited_once_with(
            self.s.client, TASK["reference_number"]
        )
        self.assertEqual(self.s.views, [KEY])
        self.assertEqual(self.s.posts, CONFIRMED)
        self.assertEqual((self.s.bulk, self.s.dialogs, self.s.unexpected), ([], [], []))
        self.assertEqual(self.s.tasks, [])
        # Find the task, then verify absence and cross-check it against My Bookings.
        self.assertEqual(self.s.reads, 3)
        self.assertEqual(self.s.listings[-1]["tasks"], [])
        self.assertIn("submitting the confirmed co-booking", self.progress())

    async def test_opens_the_visible_confirm_not_its_hidden_duplicate(self):
        _, current, row = await locate_reviewed_task(self.s.client, TASK)
        frame, button = await open_confirmation_page(self.s.client, current, row)
        # Reject + Confirm, and the same pair hidden under #panel_buttonShadow.
        self.assertEqual(
            await frame.locator('a[id$="_buttonWorkflowAction"]').count(), 4
        )
        self.assertEqual(" ".join((await button.inner_text()).split()), "Confirm")
        self.assertTrue(
            await button.evaluate("e => !!e.closest('#panel_ButtonsPanel')")
        )
        self.assertEqual(self.s.posts, [])

    async def test_a_new_confirmed_status_is_positive_evidence(self):
        self.s.after_confirm = "Confirmed"
        result = await confirm_task(self.s.client, TASK)
        self.assertEqual(result["task_status"], "Confirmed")
        self.assertEqual(self.s.posts, CONFIRMED)

    async def test_a_late_confirm_button_is_waited_for(self):
        self.s.buttons_ms = 600
        result = await confirm_task(self.s.client, TASK)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.s.posts, CONFIRMED)

    async def test_a_mixed_case_task_page_address_is_recognised(self):
        self.s.details_base = BASE + "/Modules/HomeNavigation/BookingEnquiryTask.aspx"
        result = await confirm_task(self.s.client, TASK)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.s.posts, CONFIRMED)

    async def test_only_the_reviewed_task_is_opened_and_confirmed(self):
        other = dict(
            ROW,
            key="63",
            reference_number="BK-20990101-000063",
            room="LKSLIB Project Room 4-12",
        )
        self.s.tasks.insert(0, other)
        result = await confirm_task(self.s.client, TASK)
        self.assertEqual(result["reference_number"], TASK["reference_number"])
        self.assertEqual(self.s.views, [KEY])
        self.assertEqual(self.s.posts, CONFIRMED)
        self.assertEqual([task["key"] for task in self.s.tasks], ["63"])

    async def test_verified_confirmation_is_checkpointed_before_receipt(self):
        with patch("sagasu.fbs_tasks.checkpoint_verified") as checkpoint:

            async def capture(client, reference):
                checkpoint.assert_called_once()
                self.assertEqual(checkpoint.call_args.args[0]["status"], "approved")
                return b"booking-details-png"

            with patch(
                "sagasu.fbs_tasks.capture_booking_evidence", side_effect=capture
            ) as receipt:
                result = await confirm_task(self.s.client, TASK)
        receipt.assert_awaited_once()
        self.assertEqual(result["receipt_kind"], "booking_details")

    async def test_receipt_falls_back_to_a_screenshot(self):
        self.s.evidence.side_effect = LayoutChanged("not in My Bookings")
        result = await confirm_task(self.s.client, TASK)
        self.assertEqual(result["status"], "approved")
        # What FBS showed right after Confirm, captured before the re-read.
        self.assertEqual(result["receipt_kind"], "confirmation_page")
        self.assertTrue(result["receipt_png"].startswith(b"\x89PNG"))
        self.assertEqual(self.s.posts, CONFIRMED)


class BrowserRefusalTests(StandInCase):
    """Refused before Confirm is pressed: TaskUnavailable and no POST."""

    async def test_a_page_for_another_booking(self):
        self.s.page_reference = "BK-20990101-000099"
        message = await self.refused()
        self.assertIn("different booking", message)
        self.assertIn("No confirmation was submitted", message)
        self.assertEqual(self.s.views, [KEY])
        self.assert_unsubmitted()

    async def test_a_page_without_confirm(self):
        self.s.labels = ["Reject"]
        with patch.object(fbs_tasks, "CONFIRM_PAGE_SECONDS", 1.0):
            message = await self.refused()
        self.assertIn("one Confirm", message)
        self.assert_unsubmitted()

    async def test_a_confirm_only_in_the_hidden_duplicate(self):
        self.s.labels = ["Reject"]
        self.s.shadow_labels = ["Reject", "Confirm"]
        with patch.object(fbs_tasks, "CONFIRM_PAGE_SECONDS", 1.0):
            await self.refused()
        self.assert_unsubmitted()

    async def test_two_confirms(self):
        self.s.labels = ["Reject", "Confirm", "Confirm"]
        with patch.object(fbs_tasks, "CONFIRM_PAGE_SECONDS", 1.0):
            message = await self.refused()
        self.assertIn("one Confirm", message)
        self.assert_unsubmitted()

    async def test_a_confirm_that_would_ask_a_question(self):
        for prompt in (ZERO_PENALTY, "Are you sure you want to confirm this booking?"):
            with self.subTest(prompt=prompt):
                self.s.confirm_prompt = prompt
                message = await self.refused()
                self.assertIn("No confirmation was submitted", message)
                self.assert_unsubmitted()  # never clicked: no dialog either
        self.assertEqual(self.s.tasks[0]["status"], "Pending Confirmation")

    async def test_view_that_does_not_open_the_task_page(self):
        self.s.open_page = False
        with patch.object(fbs_tasks, "CONFIRM_PAGE_SECONDS", 1.0):
            message = await self.refused()
        self.assertIn("did not open", message)
        self.assertEqual(self.s.views, [KEY])
        self.assert_unsubmitted()

    async def test_a_task_that_changed_or_is_not_confirmable(self):
        cases = (
            # FBS now lists it as awaiting approval: not a co-booker's task.
            ({"status": "Pending Approval"}, {"status": "Pending Approval"}),
            # Reviewed as pending; FBS now shows it confirmed.
            ({}, {"status": "Confirmed"}),
            # Another room than the one reviewed.
            ({"room": "LKSLIB Project Room 4-12"}, {}),
        )
        for expected, listed in cases:
            with self.subTest(expected=expected, listed=listed):
                self.s.tasks = [dict(ROW, key=KEY, **listed)]
                await self.refused({**TASK, **expected})
        self.s.tasks = []
        await self.refused()  # no longer listed at all
        self.assertEqual(self.s.views, [])
        self.assert_unsubmitted()


class BrowserUnknownOutcomeTests(StandInCase):
    """After the one press, anything unverified is TaskApprovalOutcomeUnknown."""

    async def test_a_dropped_post(self):
        self.s.drop_post = True
        with patch.object(fbs_tasks, "CONFIRM_SUBMIT_MS", 1500):
            message = await self.unknown()
        self.assertIn("Check FBS Task List", message)
        self.assertEqual(self.s.reads, 1)  # no Task List evidence was claimed

    async def test_a_task_still_pending_afterwards(self):
        self.s.after_confirm = "pending"
        await self.unknown()
        # One read to find it, then CONFIRM_CHECKS (3) bounded re-reads.
        self.assertEqual(self.s.reads, 4)
        self.assertEqual(self.s.proxy.waits.count(2000), 2)

    async def test_a_task_listed_twice_afterwards(self):
        self.s.after_confirm = "duplicate"
        await self.unknown()

    async def test_a_missing_task_on_an_incomplete_listing(self):
        self.s.incomplete_after_post = True
        await self.unknown()
        self.assertEqual(self.s.tasks, [])  # gone, but the read was not complete

    async def test_a_rejected_task_is_not_reported_as_confirmed(self):
        # Regression: any status without "pending"/"awaiting" used to count as
        # success, so a task FBS then listed as Rejected came back "approved".
        # Absence alone is also insufficient without positive evidence.
        self.s.after_confirm = "Rejected"
        await self.unknown()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def route(self, expected, approve=True):
        with (
            patch("sagasu.fbs_tasks.FBSClient"),
            patch(
                "sagasu.fbs_tasks.approve_task",
                AsyncMock(return_value={"status": "approved"}),
            ) as approve_task,
            patch(
                "sagasu.fbs_tasks.confirm_task",
                AsyncMock(return_value={"status": "approved"}),
            ) as confirm,
            patch(
                "sagasu.fbs_tasks.capture_task_details",
                AsyncMock(return_value={"status": "details"}),
            ) as details,
        ):
            await execute_task_action(expected, approve=approve)
        return approve_task, confirm, details

    async def test_a_confirm_only_task_goes_to_confirm_task(self):
        approve_task, confirm, details = await self.route(TASK)
        confirm.assert_awaited_once()
        self.assertEqual(confirm.await_args.args[1], TASK)
        approve_task.assert_not_awaited()
        details.assert_not_awaited()

    async def test_approvers_and_unactionable_tasks_go_to_approve_task(self):
        for expected in (
            APPROVER_TASK,
            {**TASK, "can_approve": True},  # both: the approver's path
            {**TASK, "can_confirm": False},  # neither: approve_task refuses
        ):
            with self.subTest(expected=expected):
                approve_task, confirm, _ = await self.route(expected)
                approve_task.assert_awaited_once()
                confirm.assert_not_awaited()

    async def test_reading_details_never_confirms(self):
        approve_task, confirm, details = await self.route(TASK, approve=False)
        details.assert_awaited_once()
        approve_task.assert_not_awaited()
        confirm.assert_not_awaited()


class BrowserRoutingTests(StandInCase):
    async def test_execute_task_action_confirms_through_the_stand_in(self):
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context):
            result = await execute_task_action(TASK, approve=True, interactive=False)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.s.posts, CONFIRMED)
        self.assertEqual(self.s.contexts[0]["interactive"], False)

    async def test_a_claimed_approval_on_this_row_presses_nothing(self):
        # Routed to approve_task, which re-reads the row: this page has no
        # Approve Selected, so it refuses, and Confirm is never reached.
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context):
            with self.assertRaises(TaskUnavailable):
                await execute_task_action(
                    {**TASK, "can_approve": True}, approve=True, interactive=False
                )
        self.assertEqual(self.s.views, [])
        self.assert_unsubmitted()


class StandInBackend(Backend):
    """The real Backend dispatch for task actions; Task List from the stand-in."""

    def __init__(self, standin):
        super().__init__(interactive_auth=False)
        self.standin, self.kinds = standin, []

    async def run(self, kind, payload, progress, notify):
        self.kinds.append(kind)
        if kind == "tasks":
            return await self.standin.read(self.standin.client)
        return await super().run(kind, payload, progress, notify)


@unittest.skipIf(Client is None, "Install Sagasu for MCP runtime tests")
class RemoteAgentTests(StandInCase):
    async def test_remote_agent_confirms_once_through_its_tools(self):
        backend = StandInBackend(self.s)
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        server = create_server(
            service, allow_agent_submission=True, remote=RemoteAccess(writes=True)
        )
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context):
            async with Client(server) as client:

                async def call(name, **arguments):
                    result = await client.call_tool(name, arguments)
                    self.assertFalse(result.is_error, result)
                    return result.structured_content

                async def finish(started):
                    while True:
                        done = await call(
                            "wait_operation",
                            operation_id=started["operation_id"],
                            timeout=30,
                        )
                        if done["terminal"]:
                            return done

                listed = await finish(await call("list_tasks"))
                self.assertEqual(listed["status"], "succeeded")
                self.assertEqual(listed["result"]["tasks"], [TASK])
                review = await call(
                    "task_approval_review",
                    tasks_operation_id=listed["operation_id"],
                    reference_number=TASK["reference_number"],
                )
                self.assertTrue(
                    review["summary"]["action"].startswith("Confirm this co-booking")
                )
                submitted = await call("submit_review", review_id=review["review_id"])
                done = await finish(submitted)
                self.assertEqual(done["status"], "succeeded", done)
                self.assertEqual(done["result"]["status"], "approved")
                self.assertEqual(
                    done["result"]["task_status"], "Confirmed (left the Task List)"
                )
                again = await call("submit_review", review_id=review["review_id"])
                self.assertEqual(again["operation_id"], submitted["operation_id"])
        self.assertEqual(self.s.posts, CONFIRMED)
        self.assertEqual(backend.kinds.count("approve_task"), 1)
        self.assertEqual(service.get(submitted["operation_id"]).receipt, PNG)
        self.assertEqual(self.s.contexts[0]["interactive"], False)


if __name__ == "__main__":
    unittest.main()
