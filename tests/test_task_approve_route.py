"""Approve Selected on the real-shaped Task List; a stand-in, never live FBS.

The stand-in is shaped like the Task List FBS served on 21 Nov 2026
(modules/homenavigation/tasklist.aspx in the iframe frameContent, sanitised):

- The shell <form id="form1" style="display: none; height:100%;"> is revealed
  by script on load.
- #GridViewTask holds the toolbar #GridViewTask_command, the header
  (#GridViewTask_header > table#GridViewTask_gv > tr.header, whose first cell
  is the select-all box GridViewTask$gv$ctl01$checkMultiple) and the data rows
  (#GridViewTask_content tr.row, nine cells, the first the row's own
  GridViewTask$gv$ctlNN$checkMultiple box, the last its View postback).
- Approve Selected and Reject Selected sit in #GridViewTask_actsonitems, served
  display:none. FBS's grid script, gridv.initgv('#GridViewTask', ...) with
  checkm and acton, is re-implemented below without jQuery: the toolbar shows
  only while a data row is ticked, the header is ticked whenever every row is
  ticked, a header change ticks or unticks every row, and a row change sets the
  header to that row's state; acton runs on init and after every change.
- Approve Selected posts form1 back (a __doPostBack on a 0 ms timer), carrying
  every ticked checkbox, the header's included. The stand-in reads which row
  boxes were posted.

What FBS does after Approve Selected for a co-booking is NOT known; the knob
after_approve models the plausible reactions: remove the posted task and
acknowledge it in #MessagePanel/#MessageLabel, mark it Confirmed, do nothing,
or acknowledge another reference.

Chromium runs behind a dead proxy and every request is routed, so nothing
leaves this machine; a redirect would be an HTML location.replace() page,
never a 3xx.
"""

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
from sagasu.fbs_client import FBS_URL
from sagasu.fbs_http import parse_account_document
from sagasu.fbs_tasks import (
    IDENTITY,
    TaskApprovalOutcomeUnknown,
    TaskUnavailable,
    add_task_capabilities,
    approval_available,
    approve_task,
    execute_task_action,
    only_one_task_selected,
)
from sagasu.mcp_service import AgentService, Backend
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
APPROVE_TARGETS = {"GridViewTask$ctl13$ctl00", "GridViewTask$ctl15$ctl00"}
REJECT_TARGET = "GridViewTask$ctl14$ctl00"
SUBMITTING = "submitting the confirmed task approval"
ACKNOWLEDGED = "Approval acknowledged by FBS"

# The one row FBS listed on 21 Nov 2026, sanitised (as in test_task_confirm).
ROW = {
    "key": "62",
    "reference_number": "BK-20990101-000062",
    "date_time": "22-Nov-2099 10:30 - 12:30 (2 hrs)",
    "building": "Li Ka Shing Library",
    "room": "LKSLIB Project Room 4-11",
    "request_type": "Booking Request",
    "requested_by": "ALEX TAN",
    "status": "Pending Confirmation",
}
OTHER = dict(
    ROW,
    key="63",
    reference_number="BK-20990101-000063",
    room="LKSLIB Project Room 4-12",
    requested_by="SAM LEE",
)


def listed(row):
    """What a read of the real page reports for this row."""
    return {
        **{key: row[key] for key in (*IDENTITY, "status")},
        "can_approve": True,
        "can_view": True,
        "can_confirm": True,
    }


TASK = listed(ROW)
OTHER_TASK = listed(OTHER)

HEADERS = [
    "Ref. Number",
    "Date &amp; Time of Use",
    "Building",
    "Facility",
    "Type of Request",
    "Requested By",
    "Status",
    "",
]


def postback(target):
    """FBS's link postback, as served: the click returns, a timer posts form1."""
    return (
        "btn.cl(this);setTimeout('__doPostBack(\\'"
        + target
        + "\\',\\'\\')', 0);return false;"
    )


def approve_link(ctl="ctl13", attrs='href="javascript:v();" class="btnblue"'):
    return (
        f'<a id="GridViewTask_{ctl}" onclick="{postback(f"GridViewTask${ctl}$ctl00")}" '
        f'onkeypress="return btn.kp();" {attrs}>'
        "<span><img align='absmiddle' src='/images/tick2.png' />"
        "&nbsp;Approve Selected</span></a>"
    )


REJECT_LINK = (
    f'<a id="GridViewTask_ctl14" onclick="{postback(REJECT_TARGET)}" '
    'onkeypress="return btn.kp();" href="javascript:v();" class="btnblue">'
    "<span><img align='absmiddle' src='/images/cross2.png' />"
    "&nbsp;Reject Selected</span></a>"
)
REAL_TOOLBAR = approve_link() + REJECT_LINK

# FBS's grid script as observed (jQuery there; plain DOM here, same behaviour):
#   gridv.acton = function (headerDiv, contentDiv, actonitemDiv) {
#     checked = content row boxes :checked; contentch = content row boxes;
#     headerch = header boxes;
#     if (checked.length > 0) show(actonitemDiv) else hide(actonitemDiv);
#     headerch.prop('checked', checked.length == contentch.length); }
# checkm runs acton on init; a header change ticks/unticks every row, then
# acton; a row change sets the header to that row's state, then acton.
# STANDIN carries the knobs: toolbar (false: acton never shows the toolbar)
# and alsoTick (another row's box some other script ticks with this one).
GRID_SCRIPT = """<script>
var btn = {cl: function () {}, kp: function () { return true; }};
function v() {}
function __doPostBack(target, argument) {
  var form = document.getElementById('form1');
  form.__EVENTTARGET.value = target;
  form.__EVENTARGUMENT.value = argument;
  form.submit();
}
var gridv = {
  boxes: function (div) {
    return Array.prototype.slice.call(
      div.querySelectorAll('input[type="checkbox"][id$="checkMultiple"]'));
  },
  acton: function (headerDiv, contentDiv, actonitemDiv) {
    var contentch = gridv.boxes(contentDiv);
    var checked = contentch.filter(function (e) { return e.checked; });
    var headerch = gridv.boxes(headerDiv);
    if (checked.length > 0) {
      if (STANDIN.toolbar) actonitemDiv.style.display = '';
    } else {
      actonitemDiv.style.display = 'none';
    }
    headerch.forEach(function (e) { e.checked = (checked.length == contentch.length); });
  },
  checkm: function (id) {
    var header = document.querySelector(id + '_header');
    var content = document.querySelector(id + '_content');
    var bar = document.querySelector(id + '_actsonitems');
    gridv.acton(header, content, bar);
    gridv.boxes(header).forEach(function (box) {
      box.addEventListener('change', function () {
        var state = this.checked;
        gridv.boxes(content).forEach(function (e) { e.checked = state; });
        gridv.acton(header, content, bar);
      });
    });
    gridv.boxes(content).forEach(function (box) {
      box.addEventListener('change', function () {
        var state = this.checked;
        gridv.boxes(header).forEach(function (e) { e.checked = state; });
        if (state && STANDIN.alsoTick && this.id !== STANDIN.alsoTick) {
          document.getElementById(STANDIN.alsoTick).checked = true;
        }
        gridv.acton(header, content, bar);
      });
    });
  },
  initgv: function (id, options) { gridv.checkm(id); }
};
</script>"""

# FBS's shell serves form1 hidden; its startup script reveals it.
STARTUP = """<script>
document.addEventListener('DOMContentLoaded', function () {
  gridv.initgv('#GridViewTask',{"scrollableRow":false,"hoverRowClass":"hover","allowToggleCheckBox":false});
  setTimeout(function () { document.getElementById('form1').style.display = ''; }, 30);
});
</script>"""


def box_id(ctl):
    return f"GridViewTask_gv_{ctl}_checkMultiple"


def task_row(ctl, task, ticked=False):
    view = postback(f"GridViewTask$gv${ctl}$BtnView$BtnView_bt")
    cells = [
        f'<input id="{box_id(ctl)}" type="checkbox" '
        f'name="GridViewTask$gv${ctl}$checkMultiple"'
        + (' checked="checked"' if ticked else "")
        + " />",
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
        f'<a id="GridViewTask_gv_{ctl}_BtnView" onclick="{view}" '
        'href="javascript:v();">View</a>',
    ]
    return '<tr class="row">' + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def task_list_page(
    tasks,
    *,
    toolbar=REAL_TOOLBAR,
    extra="",
    message="",
    ticked=(),
    toolbar_works=True,
    also_tick=None,
):
    """The Task List page for these tasks: (html, {row control: task key}).

    toolbar: the links inside #GridViewTask_actsonitems; extra: markup after
    that toolbar in #GridViewTask_command; message: an acknowledgement shown in
    #MessagePanel; ticked: task keys served already ticked (as ASP.NET
    re-renders posted checkboxes); toolbar_works / also_tick: STANDIN knobs."""
    rendered, rows = {}, []
    for index, task in enumerate(tasks):
        ctl = f"ctl{index + 2:02d}"
        rendered[ctl] = task["key"]
        rows.append(task_row(ctl, task, task["key"] in ticked))
    if not rows:
        rows.append('<tr class="row"><td colspan="9">No items</td></tr>')
    also = next((ctl for ctl, key in rendered.items() if key == also_tick), None)
    standin = {"toolbar": toolbar_works, "alsoTick": box_id(also) if also else None}
    header = (
        '<tr class="header"><th><input id="GridViewTask_gv_ctl01_checkMultiple" '
        'type="checkbox" name="GridViewTask$gv$ctl01$checkMultiple" /></th>'
        + "".join(f"<th>{label}</th>" for label in HEADERS)
        + "</tr>"
    )
    panel = (
        '<div id="MessagePanel" class="message"'
        + ("" if message else ' style="display:none;"')
        + '><a id="MessageDismiss" href="javascript:v();" onclick="document.'
        "getElementById('MessagePanel').style.display='none';return false;\">"
        + 'Dismiss</a><span id="MessageLabel">'
        + html.escape(message)
        + "</span></div>"
    )
    body = (
        panel + '<div class="search"><input name="txtSearch_c1" type="text" '
        'id="txtSearch_c1" value="" /><a id="buttonReset" href="javascript:v();">'
        "Reset</a></div>"
        '<div id="GridViewTask" class="gridv">'
        '<div id="GridViewTask_command" class="command">'
        '<span id="GridViewTask_actsonitems" style="display:none;" class="actsonitems">'
        + toolbar
        + "</span>"
        + extra
        + "</div>"
        '<div id="GridViewTask_header"><table id="GridViewTask_gv">'
        + header
        + "</table></div>"
        '<div id="GridViewTask_content"><table class="grid">'
        + "".join(rows)
        + "</table></div>"
        f'<div class="pager"><span>{len(tasks)} ITEM(S) TOTAL</span></div></div>'
    )
    page = (
        '<!doctype html><html><head><meta charset="utf-8"><title>SMU FBS</title>'
        + f"<script>var STANDIN = {json.dumps(standin)};</script>"
        + GRID_SCRIPT
        + '</head><body><div id="__divPage__">'
        + f'<form method="post" action="{html.escape(LIST_URL)}" id="form1" '
        + 'style="display: none; height:100%;">'
        + '<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />'
        + '<input type="hidden" name="__EVENTARGUMENT" id="__EVENTARGUMENT" value="" />'
        + '<input type="hidden" name="__ViewStateKey__" id="__ViewStateKey__" '
        + 'value="VSK-STANDIN" />'
        + body
        + "</form></div>"
        + STARTUP
        + "</body></html>"
    )
    return page, rendered


# The Approve Selected variants the capability tests serve.
OUTSIDE_TOOLBAR = dict(
    toolbar=REJECT_LINK,
    extra='<span id="GridViewTask_more" style="display:none;">'
    + approve_link()
    + "</span>",
)
TWO_LINKS = dict(toolbar=approve_link() + approve_link("ctl15") + REJECT_LINK)
DISABLED = {
    # ASP.NET (3.5 rendering) marks a disabled LinkButton with disabled=.
    "disabled attribute": dict(
        toolbar=approve_link(
            attrs='disabled="disabled" href="javascript:v();" class="btnblue"'
        )
        + REJECT_LINK
    ),
    # ASP.NET 4 renders a disabled LinkButton without href.
    "aspNetDisabled, no href": dict(
        toolbar=approve_link(attrs='class="aspNetDisabled btnblue"') + REJECT_LINK
    ),
}
ARIA_DISABLED = dict(
    toolbar=approve_link(
        attrs='aria-disabled="true" href="javascript:v();" class="btnblue"'
    )
    + REJECT_LINK
)
SELF_HIDDEN = dict(
    toolbar=approve_link(
        attrs='style="display:none;" href="javascript:v();" class="btnblue"'
    )
    + REJECT_LINK
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


class ApproveStandIn:
    """FBS's Task List with Approve Selected, routed in a real Chromium.

    Knobs: tasks (rows, in order, each with a stand-in key); toolbar / extra
    (see task_list_page); toolbar_works (False: ticking never shows the
    toolbar); also_tick (a task key whose box some script ticks along with
    any ticked row); ticked (task keys served already ticked); after_approve
    ("remove": the posted tasks leave the list and FBS acknowledges "N task
    approved successfully."; "confirmed": their status becomes Confirmed;
    "nothing": no change; "other_reference": removed, but the acknowledgement
    names another reference; "remove_silent": removed, no acknowledgement;
    "ack_only": acknowledged, task unchanged).

    Records: approvals [sorted posted checkbox controls] per Approve Selected
    POST (ctl01 is the header's select-all); rejections; views; dialogs;
    unexpected requests; reads; listings (each read's result).
    """

    async def setup(self, case):
        self.tasks = [dict(ROW)]
        self.toolbar, self.extra = REAL_TOOLBAR, ""
        self.toolbar_works = True
        self.also_tick = None
        self.ticked = set()
        self.after_approve = "remove"
        self.approvals, self.rejections, self.views = [], [], []
        self.dialogs, self.unexpected, self.listings, self.contexts = [], [], [], []
        self.reads = 0
        self.rendered = {}
        driver = await async_playwright().start()
        case.addAsyncCleanup(driver.stop)
        # A dead proxy: a request the routes below do not answer fails here
        # instead of reaching FBS or Microsoft.
        self.browser = await driver.chromium.launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"}
        )
        case.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.page.on("dialog", lambda dialog: self.dialogs.append(dialog.message))
        await self.page.route("**/*", self.route)
        await self.page.goto(FBS_URL)
        await self.frame().locator("#GridViewTask_content").wait_for(state="visible")
        self.proxy = PageProxy(self.page)
        self.client = NS(
            page=self.proxy, progress=AsyncMock(), _wait_for_idle=self.idle
        )
        self.evidence = AsyncMock(return_value=PNG)
        for target, value in (
            ("sagasu.fbs_tasks.read_task_list", self.read),
            ("sagasu.fbs_tasks.capture_booking_evidence", self.evidence),
        ):
            patched = patch(target, value)
            patched.start()
            case.addCleanup(patched.stop)

    def frame(self):
        return self.page.frame(name="frameContent")

    def task(self, key):
        return next((task for task in self.tasks if task["key"] == key), None)

    @staticmethod
    async def idle(frame):
        """Stands in for FBSClient._wait_for_idle (no ASP.NET AJAX here)."""
        await frame.wait_for_load_state("domcontentloaded")

    @asynccontextmanager
    async def context(self, **kwargs):
        """Stands in for FBSClient(...) in execute_task_action."""
        self.contexts.append(kwargs)
        yield self.client

    async def read(self, client, *, reference=None):
        """Stands in for read_task_list: a fresh load of the stand-in Task List."""
        self.reads += 1
        frame = self.frame()
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
        self.listings.append(copy.deepcopy(result))
        return result

    def listing(self, message=""):
        body, self.rendered = task_list_page(
            self.tasks,
            toolbar=self.toolbar,
            extra=self.extra,
            message=message,
            ticked=self.ticked,
            toolbar_works=self.toolbar_works,
            also_tick=self.also_tick,
        )
        return body

    def approved(self, keys):
        """FBS's (unknown) reaction to Approve Selected; returns its message."""
        posted = [self.task(key) for key in keys if self.task(key)]
        mode = self.after_approve
        if mode in {"remove", "remove_silent", "other_reference"}:
            for task in posted:
                self.tasks.remove(task)
        elif mode == "confirmed":
            for task in posted:
                task["status"] = "Confirmed"
        if mode == "remove":
            return f"{len(posted)} task approved successfully."
        if mode == "other_reference":
            return f"{OTHER['reference_number']} approved successfully."
        if mode == "ack_only":
            return "1 task approved successfully."
        return ""

    async def route(self, route):
        request = route.request
        url = request.url
        path = urlsplit(url).path.casefold()
        if request.method == "POST":
            fields = parse_qs(request.post_data or "", keep_blank_values=True)
            target = (fields.get("__EVENTTARGET") or [""])[0]
            boxes = sorted(
                match[1]
                for name in fields
                if (
                    match := re.fullmatch(
                        r"GridViewTask\$gv\$(ctl\d\d)\$checkMultiple", name
                    )
                )
            )
            if path == LIST_PATH:
                if target in APPROVE_TARGETS:
                    self.approvals.append(boxes)
                    message = self.approved(
                        [self.rendered[box] for box in boxes if box in self.rendered]
                    )
                    await route.fulfill(
                        body=self.listing(message), content_type="text/html"
                    )
                    return
                if target == REJECT_TARGET:
                    self.rejections.append(boxes)
                    await route.fulfill(body=self.listing(), content_type="text/html")
                    return
                view = re.fullmatch(
                    r"GridViewTask\$gv\$(ctl\d\d)\$BtnView\$BtnView_bt", target
                )
                if view:
                    self.views.append(self.rendered.get(view[1]))
                    await route.fulfill(body=self.listing(), content_type="text/html")
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
            body = self.listing()
        else:
            if not path.endswith((".png", ".ico")):
                self.unexpected.append((request.method, path, ""))
            await route.fulfill(status=404, body="")
            return
        await route.fulfill(body=body, content_type="text/html")


class StandInCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = ApproveStandIn()
        await self.s.setup(self)

    def progress(self):
        return [call.args[0] for call in self.s.client.progress.await_args_list]

    def assert_unsubmitted(self):
        self.assertEqual(self.s.approvals, [])
        self.assertEqual(self.s.rejections, [])
        self.assertEqual(self.s.views, [])
        self.assertEqual(self.s.dialogs, [])
        self.assertEqual(self.s.unexpected, [])
        self.assertNotIn(SUBMITTING, self.progress())
        self.s.evidence.assert_not_awaited()

    def assert_one_press(self, boxes):
        self.assertEqual(self.s.approvals, [boxes])
        self.assertEqual(self.s.rejections, [])
        self.assertEqual(self.s.views, [])
        self.assertEqual(self.s.dialogs, [])
        self.assertEqual(self.s.unexpected, [])

    async def unknown(self, expected=TASK):
        with self.assertRaises(TaskApprovalOutcomeUnknown) as caught:
            await approve_task(self.s.client, expected)
        self.s.evidence.assert_not_awaited()
        return str(caught.exception)

    async def refused(self, expected=TASK):
        with self.assertRaises(TaskUnavailable) as caught:
            await approve_task(self.s.client, expected)
        return str(caught.exception)

    async def boxes(self):
        """{box id: ticked} and whether the toolbar shows, as the page has them."""
        frame = self.s.frame()
        state = await frame.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll(
                '#GridViewTask input[type="checkbox"]')].map(e => [e.id, e.checked]))"""
        )
        shown = await frame.locator("#GridViewTask_actsonitems").is_visible()
        return state, shown


# -- 1. Capabilities: the browser read and the HTTP read agree ------------------


class HTTPCapabilityTests(unittest.TestCase):
    """parse_account_document('tasks', ...) on the real-shaped Task List."""

    def read(self, tasks=(ROW,), **page):
        html_page, _ = task_list_page(list(tasks), **page)
        return parse_account_document("tasks", html_page)

    def test_real_page_offers_approve_and_confirm(self):
        result = self.read()
        self.assertEqual((result["total"], result["complete"]), (1, True))
        self.assertEqual(result["tasks"], [TASK])

    def test_each_row_of_a_two_row_list_offers_approve(self):
        self.assertEqual(self.read((OTHER, ROW))["tasks"], [OTHER_TASK, TASK])

    def test_approve_hidden_outside_the_toolbar_is_not_offered(self):
        (task,) = self.read(**OUTSIDE_TOOLBAR)["tasks"]
        self.assertEqual(task, {**TASK, "can_approve": False})

    def test_two_approve_selected_links_are_not_offered(self):
        (task,) = self.read(**TWO_LINKS)["tasks"]
        self.assertEqual(task, {**TASK, "can_approve": False})

    def test_a_disabled_approve_link_is_not_offered(self):
        for name, page in {**DISABLED, "aria-disabled": ARIA_DISABLED}.items():
            with self.subTest(name):
                (task,) = self.read(**page)["tasks"]
                self.assertEqual(task, {**TASK, "can_approve": False})

    def test_approve_hidden_by_its_own_style_is_not_offered(self):
        # Only the toolbar's display:none is FBS's tick-to-show; the link's own
        # display:none is not revealed by ticking.
        (task,) = self.read(**SELF_HIDDEN)["tasks"]
        self.assertEqual(task, {**TASK, "can_approve": False})


class BrowserCapabilityTests(StandInCase):
    """add_task_capabilities on the stand-in, as read_task_list runs it."""

    async def read_with(self, **page):
        for name, value in page.items():
            setattr(self.s, name, value)
        return (await self.s.read(self.s.client))["tasks"]

    async def test_real_page_offers_approve_and_confirm(self):
        result = await self.s.read(self.s.client)
        self.assertEqual((result["total"], result["complete"]), (1, True))
        self.assertEqual(result["tasks"], [TASK])
        # As served: the toolbar hidden, nothing ticked, the header unticked.
        self.assertEqual(
            await self.boxes(),
            ({box_id("ctl01"): False, box_id("ctl02"): False}, False),
        )
        self.assertTrue(await approval_available(self.s.frame()))

    async def test_each_row_of_a_two_row_list_offers_approve(self):
        self.assertEqual(
            await self.read_with(tasks=[dict(OTHER), dict(ROW)]), [OTHER_TASK, TASK]
        )

    async def test_approve_hidden_outside_the_toolbar_is_not_offered(self):
        self.assertEqual(
            await self.read_with(**OUTSIDE_TOOLBAR), [{**TASK, "can_approve": False}]
        )

    async def test_two_approve_selected_links_are_not_offered(self):
        self.assertEqual(
            await self.read_with(**TWO_LINKS), [{**TASK, "can_approve": False}]
        )

    async def test_a_disabled_approve_link_is_not_offered(self):
        for name, page in DISABLED.items():
            with self.subTest(name):
                self.assertEqual(
                    await self.read_with(**page), [{**TASK, "can_approve": False}]
                )

    async def test_reading_with_the_row_ticked_also_offers_approve(self):
        await self.s.read(self.s.client)
        await self.s.frame().locator("#" + box_id("ctl02")).check()
        self.assertEqual(
            await self.boxes(),
            ({box_id("ctl01"): True, box_id("ctl02"): True}, True),
        )
        result = parse_tasks(await self.s.frame().evaluate(TASK_SNAPSHOT))
        await add_task_capabilities(self.s.frame(), result)
        self.assertEqual(result["tasks"], [TASK])
        self.assertEqual(self.s.approvals, [])

    # Regression (fixed after review).
    async def test_an_aria_disabled_approve_link_is_not_offered(self):
        self.assertEqual(
            await self.read_with(**ARIA_DISABLED), [{**TASK, "can_approve": False}]
        )

    # Regression (fixed after review).
    async def test_approve_hidden_by_its_own_style_is_not_offered(self):
        self.assertEqual(
            await self.read_with(**SELF_HIDDEN), [{**TASK, "can_approve": False}]
        )


class SelectionCheckTests(StandInCase):
    """only_one_task_selected against FBS's own header/row behaviour."""

    async def set_boxes(self, *ticked):
        await self.s.frame().evaluate(
            """ticked => document.querySelectorAll('#GridViewTask input[type="checkbox"]')
                .forEach(e => { e.checked = ticked.includes(e.id); })""",
            [box_id(ctl) for ctl in ticked],
        )
        return await only_one_task_selected(self.s.frame())

    async def test_single_row_list(self):
        await self.s.read(self.s.client)
        self.assertTrue(await self.set_boxes("ctl02"))
        self.assertTrue(await self.set_boxes("ctl01", "ctl02"))  # FBS's own tick
        self.assertFalse(await self.set_boxes("ctl01"))
        self.assertFalse(await self.set_boxes())

    async def test_a_paged_list_never_lets_the_header_through(self):
        # One row on this page but more tasks on others: the header's
        # select-all could mean every page, so only the row alone is accepted.
        await self.s.read(self.s.client)
        await self.s.frame().evaluate(
            r"""() => {
                const g = document.getElementById('GridViewTask');
                g.innerHTML = g.innerHTML.replace(/\b1 ITEM\(S\) TOTAL/, '2 ITEM(S) TOTAL');
            }"""
        )
        self.assertTrue(await self.set_boxes("ctl02"))
        self.assertFalse(await self.set_boxes("ctl01", "ctl02"))

    async def test_a_ticked_box_outside_the_header_and_rows_is_refused(self):
        await self.s.read(self.s.client)
        await self.s.frame().evaluate(
            """() => document.getElementById('GridViewTask').insertAdjacentHTML(
                'afterbegin', '<input type="checkbox" id="stray">')"""
        )
        await self.set_boxes("ctl02")
        await self.s.frame().evaluate(
            "() => { document.getElementById('stray').checked = true; }"
        )
        self.assertFalse(await only_one_task_selected(self.s.frame()))

    async def test_two_row_list(self):
        self.s.tasks = [dict(OTHER), dict(ROW)]
        await self.s.read(self.s.client)
        self.assertTrue(await self.set_boxes("ctl03"))
        self.assertFalse(await self.set_boxes("ctl01", "ctl03"))
        self.assertFalse(await self.set_boxes("ctl02", "ctl03"))
        self.assertFalse(await self.set_boxes("ctl01", "ctl02", "ctl03"))
        self.assertFalse(await self.set_boxes("ctl01"))


# -- 2. A single-row list --------------------------------------------------------


class SingleRowApproveTests(StandInCase):
    async def test_ticks_the_row_presses_once_and_verifies_by_acknowledgement(self):
        result = await approve_task(self.s.client, TASK)
        # The row's box and the header FBS ticked itself, nothing else.
        self.assert_one_press(["ctl01", "ctl02"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["reference_number"], TASK["reference_number"])
        self.assertEqual(result["task"], TASK)
        self.assertEqual(result["task_status"], ACKNOWLEDGED)
        self.assertFalse(result["booking_status_verified"])
        self.assertEqual(
            (result["receipt_kind"], result["receipt_png"]), ("booking_details", PNG)
        )
        self.s.evidence.assert_awaited_once_with(
            self.s.client, TASK["reference_number"]
        )
        self.assertEqual(self.s.tasks, [])
        self.assertEqual(self.s.reads, 1)  # verified on FBS's own response
        self.assertIn(SUBMITTING, self.progress())

    async def test_a_confirmed_status_verifies(self):
        self.s.after_approve = "confirmed"
        result = await approve_task(self.s.client, TASK)
        self.assert_one_press(["ctl01", "ctl02"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["task_status"], "Confirmed")

    async def test_nothing_changing_is_unknown_after_one_press(self):
        self.s.after_approve = "nothing"
        message = await self.unknown()
        self.assertIn("Check FBS Task List", message)
        self.assert_one_press(["ctl01", "ctl02"])
        self.assertEqual(self.s.tasks[0]["status"], "Pending Confirmation")
        self.assertEqual(self.s.reads, 1)

    async def test_an_acknowledgement_for_another_reference_is_unknown(self):
        self.s.after_approve = "other_reference"
        await self.unknown()
        self.assert_one_press(["ctl01", "ctl02"])

    async def test_a_removed_task_without_acknowledgement_is_unknown(self):
        self.s.after_approve = "remove_silent"
        await self.unknown()
        self.assert_one_press(["ctl01", "ctl02"])

    async def test_an_acknowledgement_while_still_pending_is_unknown(self):
        self.s.after_approve = "ack_only"
        await self.unknown()
        self.assert_one_press(["ctl01", "ctl02"])

    # Regression (fixed after review).
    async def test_a_list_served_with_its_row_ticked_is_still_approved(self):
        self.s.ticked = {ROW["key"]}
        self.s.page.set_default_timeout(3000)
        await self.s.read(self.s.client)
        self.assertEqual(
            await self.boxes(),
            ({box_id("ctl01"): True, box_id("ctl02"): True}, True),
        )
        result = await approve_task(self.s.client, TASK)
        self.assertEqual(result["status"], "approved")
        self.assert_one_press(["ctl01", "ctl02"])


# -- 3. A two-row list -----------------------------------------------------------


class TwoRowApproveTests(StandInCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The reviewed task is the second row (ctl03); OTHER is ctl02.
        self.s.tasks = [dict(OTHER), dict(ROW)]

    async def test_only_the_reviewed_row_is_ticked_and_posted(self):
        result = await approve_task(self.s.client, TASK)
        # Only ctl03: the header stayed unticked, the other row untouched.
        self.assert_one_press(["ctl03"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["task_status"], ACKNOWLEDGED)
        self.assertEqual(result["reference_number"], TASK["reference_number"])
        self.assertEqual(self.s.tasks, [OTHER])

    async def test_the_header_stays_unticked_while_the_reviewed_row_is_ticked(self):
        _, _, row = await fbs_tasks.locate_reviewed_task(self.s.client, TASK)
        await fbs_tasks.select_only_task(self.s.frame(), row)
        self.assertEqual(
            await self.boxes(),
            (
                {box_id("ctl01"): False, box_id("ctl02"): False, box_id("ctl03"): True},
                True,
            ),
        )
        self.assertTrue(await only_one_task_selected(self.s.frame()))
        self.assert_unsubmitted()

    async def test_a_script_ticking_a_second_row_during_selection_is_refused(self):
        self.s.also_tick = OTHER["key"]
        message = await self.refused()
        # select_only_task's own check caught it.
        self.assertIn("did not retain exactly one selected task", message)
        self.assertIn("No approval was submitted", message)
        self.assert_unsubmitted()

    async def changed_before_press(self, change):
        """Run approve_task; `change` runs on the page after the row is ticked
        and re-read, just before Approve Selected would be pressed."""
        task_rows = fbs_tasks.task_rows

        async def rows_then_change(frame, result=None):
            if result is None:  # approve_task's re-read after select_only_task
                await frame.evaluate(change)
            return await task_rows(frame, result)

        with patch("sagasu.fbs_tasks.task_rows", rows_then_change):
            return await self.refused()

    async def test_a_second_row_ticked_before_the_press_is_refused(self):
        # A click, so FBS's grid script reacts (header ticked: every row is).
        message = await self.changed_before_press(
            f"() => document.getElementById('{box_id('ctl02')}').click()"
        )
        # approve_task's last check before the press caught it.
        self.assertIn("selection changed before submission", message)
        self.assertIn("No approval was submitted", message)
        self.assert_unsubmitted()

    async def test_the_header_ticked_before_the_press_is_refused(self):
        for change in (
            # Ticked by script: the header alone, one row still ticked.
            f"() => {{ document.getElementById('{box_id('ctl01')}').checked = true; }}",
            # Clicked: FBS ticks every row.
            f"() => document.getElementById('{box_id('ctl01')}').click()",
        ):
            with self.subTest(change=change):
                message = await self.changed_before_press(change)
                self.assertIn("selection changed before submission", message)
                self.assertIn("No approval was submitted", message)
                self.assert_unsubmitted()


# -- 4. A toolbar that never appears ----------------------------------------------


class ToolbarTests(StandInCase):
    async def test_a_toolbar_that_never_appears_is_refused_unsubmitted(self):
        self.s.toolbar_works = False
        # Still offered: the link is the one in FBS's tick-to-show toolbar.
        self.assertEqual((await self.s.read(self.s.client))["tasks"], [TASK])
        message = await self.refused()
        self.assertIn("approval action changed", message)
        self.assert_unsubmitted()
        self.assertEqual(self.s.tasks, [ROW])


# -- 5. Routing --------------------------------------------------------------------


class RoutingTests(StandInCase):
    async def test_a_task_read_from_the_real_page_goes_to_approve_task(self):
        (task,) = (await self.s.read(self.s.client))["tasks"]
        self.assertTrue(task["can_approve"] and task["can_confirm"])
        never = AsyncMock(side_effect=AssertionError("confirm_task was called"))
        with (
            patch("sagasu.fbs_tasks.FBSClient", self.s.context),
            patch("sagasu.fbs_tasks.confirm_task", never),
        ):
            result = await execute_task_action(task, approve=True, interactive=False)
        never.assert_not_awaited()
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["task_status"], ACKNOWLEDGED)
        # Approve Selected once; never View, never the task's own page.
        self.assert_one_press(["ctl01", "ctl02"])
        self.assertEqual(
            self.s.contexts,
            [{"auth_notify": None, "progress": None, "interactive": False}],
        )


# -- 6. Through AgentService as a remote caller --------------------------------------


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


class RemoteServiceTests(StandInCase):
    async def test_remote_caller_approves_once_and_never_replays(self):
        backend = StandInBackend(self.s)
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)

        async def finish(job):
            await service.get(job["operation_id"]).task
            return service.get(job["operation_id"])

        with patch("sagasu.fbs_tasks.FBSClient", self.s.context), remote_origin():
            listing = await finish(service.start("tasks"))
            self.assertEqual(listing.result["tasks"], [TASK])
            review = service.review_task_approval(listing.id, TASK["reference_number"])
            self.assertTrue(
                review["summary"]["action"].startswith("Approve this task only"),
                review["summary"]["action"],
            )
            self.assertEqual(review["summary"]["task"], TASK)
            submitted = service.submit(review["review_id"])
            done = await finish(submitted)
            self.assertEqual(done.status, "succeeded", done.public())
            self.assertEqual(done.result["status"], "approved")
            self.assertEqual(done.result["task_status"], ACKNOWLEDGED)
            again = service.submit(review["review_id"])
            self.assertEqual(again["operation_id"], submitted["operation_id"])
        self.assert_one_press(["ctl01", "ctl02"])
        self.assertEqual(backend.kinds.count("approve_task"), 1)
        self.assertEqual(done.receipt, PNG)
        self.assertEqual(self.s.contexts[0]["interactive"], False)


@unittest.skipIf(Client is None, "Install Sagasu for MCP runtime tests")
class RemoteAgentTests(StandInCase):
    async def test_remote_agent_approves_once_through_its_tools(self):
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

                listing = await finish(await call("list_tasks"))
                self.assertEqual(listing["status"], "succeeded")
                self.assertEqual(listing["result"]["tasks"], [TASK])
                review = await call(
                    "task_approval_review",
                    tasks_operation_id=listing["operation_id"],
                    reference_number=TASK["reference_number"],
                )
                self.assertTrue(
                    review["summary"]["action"].startswith("Approve this task only")
                )
                submitted = await call("submit_review", review_id=review["review_id"])
                done = await finish(submitted)
                self.assertEqual(done["status"], "succeeded", done)
                self.assertEqual(done["result"]["status"], "approved")
                self.assertEqual(done["result"]["task_status"], ACKNOWLEDGED)
                again = await call("submit_review", review_id=review["review_id"])
                self.assertEqual(again["operation_id"], submitted["operation_id"])
        self.assert_one_press(["ctl01", "ctl02"])
        self.assertEqual(backend.kinds.count("approve_task"), 1)
        self.assertEqual(service.get(submitted["operation_id"]).receipt, PNG)
        self.assertEqual(self.s.contexts[0]["interactive"], False)


if __name__ == "__main__":
    unittest.main()
