"""FBS asks again in its own "Approve Selected" box; a stand-in, never live FBS.

The stand-in follows what a live browser session showed on 21 Nov 2026
(sanitised):

1. Task List (modules/homenavigation/tasklist.aspx in the iframe frameContent,
   itself inside the iframe frameBottom). Nothing ticked: the toolbar
   #GridViewTask_actsonitems is hidden. Ticking the single row shows APPROVE
   SELECTED / REJECT SELECTED, and FBS's grid script ticks the header box too.
2. Approve Selected (a#GridViewTask_ctl13, GridViewTask$ctl13$ctl00) approves
   nothing. The page comes back (ASP.NET UpdatePanels: panelMain_up,
   popupDialogPanel_up, popupDialog_opc_up, popupDialog_p2_up, ...) with both
   boxes rendered checked="checked", the toolbar visible, and the box BOX
   (verbatim below) in <span id="popupDialogPanel_sp">: title "Approve
   Selected", an optional Comments textarea (textComments_c1), "[x close]"
   (popupDialog_bc) and "Confirm" (popupDialog_b1, a plain postback with no
   browser prompt). FBS's CSS upper-cases the button labels on screen.
3. [x close] (popupDialog_bc$popupDialog_bc_bt) removes #popupDialog
   (popupDialogPanel_sp becomes NO_BOX); the task stays Pending Confirmation,
   both boxes stay ticked, the toolbar stays visible. Nothing was approved.
4. The message area is on the page, hidden (MESSAGE_PANEL, verbatim).
5. My Bookings (modules/homenavigation/mybookingstab.aspx, rows of 9+ cells:
   [1] reference, [2] date/time, [4] room, [8] status, "N ITEM(S) TOTAL") does
   not list the pending co-booking; a co-booking appears there once accepted.

NOT observed: what FBS shows after CONFIRM (popupDialog_b1$popupDialog_b1_bt).
The knob after_confirm models the plausible reactions (see BoxStandIn).

The stand-in's own reading of that, not observations: being UpdatePanels, a
postback replaces panels in place and never loads the frame again (here the
message area, panelMain_up and popupDialogPanel_up); the box's backdrop
(.dlgbg) covers the page; the address of the frameBottom document. One
scenario (full_postbacks) has FBS answer CONFIRM with a whole page load
instead.

Chromium runs behind a dead proxy and every request is routed, so nothing
leaves this machine; no route answers with a 3xx.
"""

import asyncio
import copy
import html
import json
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from sagasu import fbs_tasks
from sagasu.fbs_account import TASK_SNAPSHOT, read_booking_page
from sagasu.fbs_client import FBS_URL, FBSClient, LayoutChanged
from sagasu.fbs_tasks import (
    TaskApprovalOutcomeUnknown,
    TaskUnavailable,
    approval_box,
    approve_task,
    close_approval_box,
    left_tasks_and_listed,
    only_one_task_selected,
)
from sagasu.mcp_service import AgentError, AgentService
from tests import test_task_approve_route as base
from tests.test_remote import remote_origin

try:
    from mcp import Client
    from sagasu.mcp_server import RemoteAccess, create_server
except ImportError:
    Client = None

BASE = base.BASE
LIST_PATH, LIST_URL = base.LIST_PATH, base.LIST_URL
BOOKINGS_PATH = "/modules/homenavigation/mybookingstab.aspx"
BOOKINGS_URL = BASE + BOOKINGS_PATH
NAVIGATION_URL = BASE + "/frameBottom"  # the stand-in's own name for that document
APPROVE_TARGET = "GridViewTask$ctl13$ctl00"
CONFIRM_TARGET = "popupDialog_b1$popupDialog_b1_bt"
CLOSE_TARGET = "popupDialog_bc$popupDialog_bc_bt"
VIEW_TARGET = re.compile(r"GridViewTask\$gv\$(ctl\d\d)\$BtnView\$BtnView_bt")
ROW_BOX = re.compile(r"GridViewTask\$gv\$(ctl\d\d)\$checkMultiple")

ROW, OTHER, TASK, OTHER_TASK, PNG = (
    base.ROW,
    base.OTHER,
    base.TASK,
    base.OTHER_TASK,
    base.PNG,
)
REFERENCE = TASK["reference_number"]
# A task that reaches the list between the two postbacks.
ARRIVAL = dict(
    ROW,
    key="64",
    reference_number="BK-20990101-000064",
    room="LKSLIB Project Room 4-13",
    requested_by="JAMIE NG",
)
# This account's own booking: My Bookings is never empty.
OWN_BOOKING = {
    "reference_number": "BK-20990101-000007",
    "date_time": "23-Nov-2099 (Mon) 09:00 - 10:00 (1hr)",
    "room": "LKSLIB Project Room 4-10",
    "fbs_status": "Confirmed",
}
SINGLE = ["ctl01", "ctl02"]  # the row's box and the header FBS ticks itself
SECOND = ["ctl03"]  # the reviewed row of [OTHER, ROW]; the header stays clear
ACKNOWLEDGED = base.ACKNOWLEDGED
SUBMITTING, CONFIRMING, VERIFIED, NOT_SUBMITTED, UNKNOWN = (
    "tasks.approval_submitting",
    "tasks.approval_confirming",
    "tasks.approval_verified",
    "tasks.approval_not_submitted",
    "tasks.approval_outcome_unknown",
)
ZERO_PENALTY = (
    "A Cancellation Penalty of [0.00] will be imposed. "
    "Please click Ok if you wish to proceed"
)


def listed_as(status):
    return "Left the Task List; My Bookings shows " + status


def booking_of(task, status):
    return {
        "reference_number": task["reference_number"],
        "date_time": task["date_time"],
        "room": task["room"],
        "fbs_status": status,
    }


# -- The observed markup, verbatim ---------------------------------------------

BOX = (
    r"""<div id="popupDialog" button1fontbold="true"> """
    r"""<div id="popupDialog_bg" class="dlgbg" style="width: 890px; height: 994px;"> """
    r"""<span id="popupDialog_opc" style="max-width: 500px; width: 500px; left: 195px; top: 340px; position: absolute; z-index: 10000;">"""
    r"""<span id="popupDialog_opc_up"><span id="popupDialog_opc_sp">"""
    r"""<div class="dialog" style="width:100%;clear:both;max-width:500px;"> """
    r"""<div id="popupDialog_p1" class="dlgheader"> """
    r"""<span id="popupDialog_l1" class="title">Approve Selected</span>"""
    r"""<a id="popupDialog_bc" title="[x close]" onclick="btn.cl(this);setTimeout('__doPostBack(\'popupDialog_bc$popupDialog_bc_bt\',\'\')', 0);return false;" onkeypress="return btn.kp();" href="javascript:v();" class="btn close"><span>[x close]</span></a> """
    r"""</div><span id="popupDialog_p2"><span id="popupDialog_p2_up"><span id="popupDialog_p2_sp">"""
    r"""<div class="dlgmain" style="clear:both;overflow:auto;max-height:430px;"> """
    r"""<div class="field full" id="textComments" title="Enter your comment here." style="font-size:Small;"> """
    r"""<div class="table"> <div class="caption top left"> <span>Comments:</span> </div>"""
    r"""<div class="control"> <span><textarea name="textComments_c1" rows="5" cols="20" id="textComments_c1" class="textbox" onchange="__trMod();" onkeydown="" onkeyup=""></textarea></span> </div> """
    r"""</div> </div> <br> </div></span></span></span>"""
    r"""<div id="popupDialog_p3" class="dlgbuttons"> """
    r"""<a id="popupDialog_b1" onclick="btn.cl(this);setTimeout('__doPostBack(\'popupDialog_b1$popupDialog_b1_bt\',\'\')', 0);return false;" onkeypress="return btn.kp();" href="javascript:v();" title="" class="btn"><span><img align="absmiddle" src="/images/tick.gif">&nbsp;Confirm</span></a> """
    r"""</div> </div></span></span></span> </div> </div>"""
)
NO_BOX = '<div style="clear:both;"> </div>'
MESSAGE_PANEL = (
    r"""<div id="PanelMessage"><div id="MessagePanel" class="object-message" style="width: 100%; top: 70px; left: 0px; right: 0px; display: none; position: fixed;"> """
    r"""<div class="div-message text-right"> <a id="linkClose" class="hlink-white" onclick="$('#MessagePanel').fadeOut();" href="javascript:void(0);">Dismiss</a> </div> """
    r"""<div class="div-message" style="max-width: 800px; margin: 0px auto; max-height: 500px; overflow: auto;"> <span id="MessageLabel"></span> </div> """
    r"""</div></div>"""
)


def variant(old, new, markup=BOX):
    """The observed markup with exactly one thing changed."""
    assert markup.count(old) == 1, old
    return markup.replace(old, new)


CONFIRM_LINK = re.search(r'<a id="popupDialog_b1".*?</a>', BOX)[0]
INNER_DIALOG = re.search(r'<div class="dialog".*</div>(?=</span></span></span>)', BOX)[
    0
]
REJECT_TITLE = variant(">Approve Selected</span>", ">Reject Selected</span>")
SUBMIT_LABEL = variant("&nbsp;Confirm</span>", "&nbsp;Submit</span>")
NO_TITLE = variant(">Approve Selected</span>", "></span>")
NO_CONFIRM = variant(CONFIRM_LINK, "")
# Two links answering to a#popupDialog_b1.
TWO_CONFIRMS = variant(CONFIRM_LINK, CONFIRM_LINK + " " + CONFIRM_LINK)
# FBS's dialog numbers its buttons (button1fontbold; popupDialog_b1), so a
# second button would be popupDialog_b2. Here it also reads "Confirm".
SECOND_BUTTON = variant(
    CONFIRM_LINK,
    CONFIRM_LINK + " " + CONFIRM_LINK.replace("popupDialog_b1", "popupDialog_b2"),
)
# A second dialog inside the same #popupDialog, under its own ids.
TWO_DIALOGS = variant(
    INNER_DIALOG, INNER_DIALOG + INNER_DIALOG.replace("popupDialog_", "popupDialog2_")
)
# A second #popupDialog, higher up the page.
TWO_BOXES = BOX + variant("top: 340px;", "top: 40px;")
HIDDEN = {
    "display:none": variant(
        '<div id="popupDialog" button1fontbold="true">',
        '<div id="popupDialog" button1fontbold="true" style="display:none;">',
    ),
    "visibility:hidden": variant(
        '<div id="popupDialog" button1fontbold="true">',
        '<div id="popupDialog" button1fontbold="true" style="visibility:hidden;">',
    ),
}
UNRECOGNISED = {
    "title Reject Selected": REJECT_TITLE,
    "no title": NO_TITLE,
    "CONFIRM labelled Submit": SUBMIT_LABEL,
    "no CONFIRM": NO_CONFIRM,
    "two CONFIRM links": TWO_CONFIRMS,
    "two dialogs in the box": TWO_DIALOGS,
    "two boxes": TWO_BOXES,
}


def asking(script):
    """The box, its CONFIRM running `script` before the postback."""
    opening = '<a id="popupDialog_b1" onclick="'
    return variant(opening, opening + html.escape(script))


def guarded(prompt):
    """A confirm() guard written as FBS writes its penalty prompt elsewhere."""
    return asking(f"if(!confirm('{prompt}'))return false;")


ASKING = {
    "FBS's zero-penalty prompt": guarded(ZERO_PENALTY),
    "an unrecognised question": guarded("Approve the selected task(s)?"),
    "another shape of confirm()": asking("if(confirm('Approve?')==false)return false;"),
}


def message_panel(message=""):
    if not message:
        return MESSAGE_PANEL
    shown = variant(" display: none;", "", MESSAGE_PANEL)
    label = '<span id="MessageLabel">'
    return variant(label, label + html.escape(message), shown)


STYLE = """<style>
/* Observed: FBS's CSS upper-cases these labels on screen. */
a.btn, a.btnblue, th { text-transform: uppercase; }
/* The stand-in's guess: the box's backdrop covers the page. */
.dlgbg { position: absolute; left: 0; top: 0; background: rgba(0, 0, 0, 0.2); }
.dialog { background: #fff; border: 1px solid #000; }
</style>"""

# What the observed markup calls besides the grid script: __trMod, its one
# jQuery call, and __doPostBack. A postback here is an UpdatePanel's: it posts
# form1 in the background, replaces the panels in place (the frame is never
# reloaded) and runs the grid script on the new rows. ASP.NET's page request
# manager reports the request in flight, as FBSClient._wait_for_idle asks it.
# The links call __doPostBack on a 0 ms timer, so for an instant after a click
# the page still looks idle, as the real one does.
BOX_SCRIPT = """<script>
function __trMod() {}
function $(selector) {
  var element = document.querySelector(selector);
  return {fadeOut: function () { element.style.display = 'none'; }};
}
var Sys = {WebForms: {PageRequestManager: {
  busy: 0,
  getInstance: function () {
    return {get_isInAsyncPostBack: function () {
      return Sys.WebForms.PageRequestManager.busy > 0;
    }};
  }
}}};
__doPostBack = function (target, argument) {
  var form = document.getElementById('form1');
  form.__EVENTTARGET.value = target;
  form.__EVENTARGUMENT.value = argument;
  if (STANDIN.fullPostbacks.indexOf(target) >= 0) {
    // Not an UpdatePanel's postback: the frame loads the answer as a new page.
    window.STANDIN_LEAVING = true;
    form.submit();
    return;
  }
  var manager = Sys.WebForms.PageRequestManager;
  manager.busy += 1;
  fetch(form.action, {method: 'POST', body: new URLSearchParams(new FormData(form))})
    .then(function (response) { return response.text(); })
    .then(function (text) {
      var next = new DOMParser().parseFromString(text, 'text/html');
      ['PanelMessage', 'panelMain_up', 'popupDialogPanel_up'].forEach(function (id) {
        document.getElementById(id).innerHTML = next.getElementById(id).innerHTML;
      });
      gridv.initgv('#GridViewTask', {});
    })
    .finally(function () { manager.busy -= 1; });
};
</script>"""
IDLE = """() => !window.Sys
    || !Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack()"""


def document(body, head=""):
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>SMU FBS</title>'
        + STYLE
        + head
        + "</head><body>"
        + body
        + "</body></html>"
    )


def task_list_page(
    tasks,
    *,
    ticked=(),
    header=False,
    box="",
    message="",
    total=None,
    full_postbacks=(),
):
    """The Task List page: (html, {row control: task key}).

    ticked: row controls (ctlNN) rendered checked="checked", and header the
    select-all box, as ASP.NET re-renders posted checkboxes; box: the markup in
    popupDialogPanel_sp; message: an acknowledgement shown in #MessagePanel;
    total: the pager's count when it is not the number of rows;
    full_postbacks: event targets the page posts as a whole page load."""
    rendered, rows = {}, []
    for index, task in enumerate(tasks):
        ctl = f"ctl{index + 2:02d}"
        rendered[ctl] = task["key"]
        rows.append(base.task_row(ctl, task, ctl in ticked))
    if not rows:
        rows.append('<tr class="row"><td colspan="9">No items</td></tr>')
    head = (
        '<tr class="header"><th><input id="GridViewTask_gv_ctl01_checkMultiple" '
        'type="checkbox" name="GridViewTask$gv$ctl01$checkMultiple"'
        + (' checked="checked"' if header else "")
        + " /></th>"
        + "".join(f"<th>{label}</th>" for label in base.HEADERS)
        + "</tr>"
    )
    count = len(tasks) if total is None else total
    grid = (
        '<div class="search"><input name="txtSearch_c1" type="text" '
        'id="txtSearch_c1" value="" /><a id="buttonReset" href="javascript:v();">'
        "Reset</a></div>"
        '<div id="GridViewTask" class="gridv">'
        '<div id="GridViewTask_command" class="command">'
        '<span id="GridViewTask_actsonitems" style="display:none;" class="actsonitems">'
        + base.REAL_TOOLBAR
        + "</span></div>"
        '<div id="GridViewTask_header"><table id="GridViewTask_gv">'
        + head
        + "</table></div>"
        '<div id="GridViewTask_content"><table class="grid">'
        + "".join(rows)
        + "</table></div>"
        f'<div class="pager"><span>{count} ITEM(S) TOTAL</span></div></div>'
    )
    standin = {
        "toolbar": True,
        "alsoTick": None,
        "fullPostbacks": list(full_postbacks),
    }
    page = document(
        '<div id="__divPage__">'
        + f'<form method="post" action="{html.escape(LIST_URL)}" id="form1" '
        + 'style="display: none; height:100%;">'
        + '<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />'
        + '<input type="hidden" name="__EVENTARGUMENT" id="__EVENTARGUMENT" value="" />'
        + '<input type="hidden" name="__ViewStateKey__" id="__ViewStateKey__" '
        + 'value="VSK-STANDIN" />'
        + message_panel(message)
        + '<span id="panelMain_up">'
        + grid
        + "</span>"
        + '<span id="popupDialogPanel_up"><span id="popupDialogPanel_sp">'
        + (box or NO_BOX)
        + "</span></span>"
        + "</form></div>"
        + base.STARTUP,
        f"<script>var STANDIN = {json.dumps(standin)};</script>"
        + base.GRID_SCRIPT
        + BOX_SCRIPT,
    )
    return page, rendered


def bookings_page(bookings):
    """My Bookings in the observed shape: [1] reference, [2] date/time,
    [4] room, [8] status, then the row's View link."""
    rows = []
    for index, booking in enumerate(bookings):
        values = [
            "",
            booking["reference_number"],
            booking["date_time"],
            "",
            booking["room"],
            "",
            "",
            "",
            booking["fbs_status"],
        ]
        cells = "".join(f"<td>{html.escape(value)}</td>" for value in values)
        cells += (
            f'<td><a id="GridViewBooking_gv_ctl{index + 2:02d}_BtnView" '
            'href="javascript:void(0);">View</a></td>'
        )
        rows.append(f'<tr class="row">{cells}</tr>')
    if not rows:
        rows.append('<tr class="row"><td colspan="10">No items</td></tr>')
    return document(
        '<form method="post" id="form1"><div id="GridViewBooking" class="gridv">'
        '<div id="GridViewBooking_content"><table class="grid">'
        + "".join(rows)
        + "</table></div>"
        f'<div class="pager"><span>{len(bookings)} ITEM(S) TOTAL</span></div>'
        "</div></form>"
    )


class PageProxy(base.PageProxy):
    """The real page, with approve_task's 500 ms pauses cut to `pause` ms."""

    pause = 50

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)
        await self._page.wait_for_timeout(min(ms, self.pause))


class BoxStandIn(base.ApproveStandIn):
    """FBS's Task List, its Approve Selected box and My Bookings, routed.

    The page keeps, between postbacks, what ASP.NET keeps: which checkboxes
    were posted ticked (by control, ctlNN) and whether the box is open. A
    fresh GET of the Task List has nothing ticked and no box.

    Knobs:
      tasks, bookings   the rows of each list, in order
      box               the markup Approve Selected opens (BOX as observed)
      on_open           how the list comes back with the box:
                        None: the posted boxes ticked, as observed;
                        "all_ticked": every row ticked;
                        "none_ticked": no row ticked;
                        "arrival": ARRIVAL is now the first row and each tick
                        stays on its control, so on the row above
      after_confirm     FBS's reaction to CONFIRM, never observed:
                        "acknowledged" (a): the posted tasks leave the list and
                          #MessageLabel shows "N task approved successfully.";
                        "listed" (b, c, e): they leave, no message, and My
                          Bookings lists each as listed_status;
                        "unlisted" (d): they leave, no message, My Bookings
                          unchanged;
                        "status" (f): they stay, their status status_after;
                        "nothing" (g): no change, the box closed;
                        "box_stays" (h): no change, the box still open;
                        "listed_incomplete" (i): as "listed", but the Task List
                          now counts one more item than the rows it shows;
                        "listed_twice": as "listed", twice over
      stale_message     an acknowledgement a fresh Task List load still shows
      full_postbacks    event targets FBS answers with a whole page load (the
                        frame is replaced), each held back for `hold` seconds

    Records: approvals / closes / confirms (the sorted checkbox controls each
    such POST carried; ctl01 is the header), comments (textComments_c1 of each
    CONFIRM, None when absent), acted (references FBS acted on at CONFIRM),
    rejections, views, dialogs, unexpected, reads, listings, booking_reads.
    """

    async def setup(self, case, *, real_idle=False):
        self.tasks = [dict(ROW)]
        self.bookings = [dict(OWN_BOOKING)]
        self.box = BOX
        self.on_open = None
        self.after_confirm = "acknowledged"
        self.listed_status = "Confirmed"
        self.status_after = "Confirmed"
        self.stale_message = ""
        self.ticked, self.header, self.box_open = set(), False, False
        self.hidden_items = 0
        self.full_postbacks, self.hold = [], 1.0
        self.approvals, self.closes, self.confirms, self.comments = [], [], [], []
        self.acted, self.rejections, self.views = [], [], []
        self.dialogs, self.unexpected, self.listings, self.contexts = [], [], [], []
        self.booking_reads, self.reads, self.rendered = [], 0, {}
        driver = await async_playwright().start()
        case.addAsyncCleanup(driver.stop)
        # A dead proxy: a request the routes below do not answer fails here
        # instead of reaching FBS or Microsoft.
        self.browser = await driver.chromium.launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"}
        )
        case.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page(viewport={"width": 1300, "height": 900})
        self.page.on("dialog", lambda dialog: self.dialogs.append(dialog.message))
        await self.page.route("**/*", self.route)
        await self.page.goto(FBS_URL)
        await self.frame().locator("#GridViewTask_content").wait_for(state="visible")
        self.proxy = PageProxy(self.page)
        self.client = NS(
            page=self.proxy,
            progress=AsyncMock(),
            _wait_for_idle=self.real_idle if real_idle else self.idle,
        )
        self.evidence = AsyncMock(return_value=PNG)
        for target, value in (
            ("sagasu.fbs_tasks.read_task_list", self.read),
            ("sagasu.fbs_account.read_bookings", self.read_bookings),
            ("sagasu.fbs_tasks.capture_booking_evidence", self.evidence),
        ):
            patched = patch(target, value)
            patched.start()
            case.addCleanup(patched.stop)

    @staticmethod
    async def idle(frame):
        """Stands in for FBSClient._wait_for_idle: no postback in flight. It
        does not wait out the 0 ms timer before one, so a pass of approve_task
        may still see the page as it was."""
        await frame.wait_for_function(IDLE, timeout=10000)

    @staticmethod
    async def real_idle(frame):
        """FBSClient._wait_for_idle itself, asking the page request manager."""
        await FBSClient._wait_for_idle(None, frame)

    async def read_bookings(self, client, *, reference=None):
        """Stands in for read_bookings: a fresh load of the stand-in My Bookings."""
        self.booking_reads.append(reference)
        frame = self.frame()
        for attempt in range(5):
            try:
                await frame.goto(BOOKINGS_URL)
                break
            except PlaywrightError:
                if attempt == 4:
                    raise
        await frame.locator("#GridViewBooking_content").wait_for(
            state="visible", timeout=10000
        )
        return copy.deepcopy(await read_booking_page(frame))

    def listing(self, message=""):
        body, self.rendered = task_list_page(
            self.tasks,
            ticked=self.ticked,
            header=self.header,
            box=self.box if self.box_open else "",
            message=message,
            total=len(self.tasks) + self.hidden_items,
            full_postbacks=self.full_postbacks,
        )
        return body

    def opened(self):
        """Approve Selected: FBS opens its box and approves nothing."""
        self.box_open = True
        if self.on_open == "all_ticked":
            self.ticked = {f"ctl{index + 2:02d}" for index in range(len(self.tasks))}
        elif self.on_open == "none_ticked":
            self.ticked, self.header = set(), False
        elif self.on_open == "arrival":
            self.tasks.insert(0, dict(ARRIVAL))

    def confirmed(self, keys):
        """FBS's (not yet observed) reaction to CONFIRM; returns its message."""
        posted = [self.task(key) for key in keys if self.task(key)]
        mode = self.after_confirm
        self.box_open = mode == "box_stays"
        if mode in {"nothing", "box_stays"}:
            return ""
        self.acted.extend(task["reference_number"] for task in posted)
        if mode == "status":
            for task in posted:
                task["status"] = self.status_after
            return ""
        for task in posted:
            self.tasks.remove(task)
            if mode.startswith("listed"):
                copies = 2 if mode == "listed_twice" else 1
                self.bookings.extend(
                    booking_of(task, self.listed_status) for _ in range(copies)
                )
        self.ticked, self.header = set(), False
        if mode == "listed_incomplete":
            self.hidden_items = 1
        if mode == "acknowledged":
            return f"{len(posted)} task approved successfully."
        return ""

    async def route(self, route):
        request = route.request
        url = request.url
        path = urlsplit(url).path.casefold()
        if request.method == "POST":
            fields = parse_qs(request.post_data or "", keep_blank_values=True)
            target = (fields.get("__EVENTTARGET") or [""])[0]
            boxes = sorted(
                match[1] for name in fields if (match := ROW_BOX.fullmatch(name))
            )
            view = VIEW_TARGET.fullmatch(target)
            if path != LIST_PATH or not (
                view
                or target
                in {APPROVE_TARGET, CLOSE_TARGET, CONFIRM_TARGET, base.REJECT_TARGET}
            ):
                self.unexpected.append((request.method, path, target))
                await route.fulfill(status=400, body="unexpected post")
                return
            keys = [self.rendered[box] for box in boxes if box in self.rendered]
            # ASP.NET renders every posted checkbox ticked again.
            self.header = "ctl01" in boxes
            self.ticked = set(boxes) - {"ctl01"}
            message = ""
            if target == APPROVE_TARGET:
                self.approvals.append(boxes)
                self.opened()
            elif target == CLOSE_TARGET:
                self.closes.append(boxes)
                self.box_open = False
            elif target == CONFIRM_TARGET:
                self.confirms.append(boxes)
                self.comments.append((fields.get("textComments_c1") or [None])[0])
                message = self.confirmed(keys)
            elif target == base.REJECT_TARGET:
                self.rejections.append(boxes)
            else:
                self.views.append(self.rendered.get(view[1]))
            body = self.listing(message)
            if request.resource_type == "document":
                # A whole page load: reads go on against the old page meanwhile.
                await asyncio.sleep(self.hold)
            await route.fulfill(body=body, content_type="text/html")
            return
        if url == FBS_URL:
            body = document(
                f'<iframe name="frameBottom" id="frameBottom" src="{NAVIGATION_URL}" '
                'style="width:1260px;height:860px;border:0"></iframe>'
            )
        elif url == NAVIGATION_URL:
            body = document(
                '<div id="TaskListTab">Task List</div>'
                '<div id="MyBookingsTab">My Bookings</div>'
                f'<iframe name="frameContent" id="frameContent" src="{LIST_URL}" '
                'style="width:1200px;height:800px"></iframe>'
            )
        elif path == LIST_PATH:
            # A fresh load: nothing ticked, no box.
            self.ticked, self.header, self.box_open = set(), False, False
            body = self.listing(self.stale_message)
        elif path == BOOKINGS_PATH:
            body = bookings_page(self.bookings)
        else:
            if not path.endswith((".png", ".gif", ".ico")):
                self.unexpected.append((request.method, path, ""))
            await route.fulfill(status=404, body="")
            return
        await route.fulfill(body=body, content_type="text/html")


class BoxCase(unittest.IsolatedAsyncioTestCase):
    REAL_IDLE = False

    async def asyncSetUp(self):
        self.s = BoxStandIn()
        await self.s.setup(self, real_idle=self.REAL_IDLE)
        self.events = []
        emit = fbs_tasks.event

        def record(name, **fields):
            self.events.append((name, fields))
            emit(name, **fields)

        self.checkpoint = Mock(wraps=fbs_tasks.checkpoint_verified)
        for target, value in (
            ("sagasu.fbs_tasks.event", record),
            ("sagasu.fbs_tasks.checkpoint_verified", self.checkpoint),
        ):
            patched = patch(target, value)
            patched.start()
            self.addCleanup(patched.stop)

    def names(self):
        return [name for name, _ in self.events]

    def progress(self):
        return [call.args[0] for call in self.s.client.progress.await_args_list]

    def quick(self):
        """For a run that takes all 60 of approve_task's passes."""
        self.s.proxy.pause = 5

    def assert_posts(self, *, approvals=(), confirms=(), closes=0):
        """Exactly these Approve Selected and CONFIRM posts; nothing else."""
        self.assertEqual(self.s.approvals, list(approvals))
        self.assertEqual(self.s.confirms, list(confirms))
        self.assertEqual(self.s.comments, [""] * len(confirms))
        self.assertEqual(len(self.s.closes), closes, self.s.closes)
        self.assertEqual(self.s.rejections, [])
        self.assertEqual(self.s.views, [])
        self.assertEqual(self.s.dialogs, [])
        self.assertEqual(self.s.unexpected, [])

    def assert_untouched(self, *tasks):
        """FBS acted on nothing: these tasks are listed as they were."""
        self.assertEqual(self.s.acted, [])
        self.assertEqual(self.s.tasks, list(tasks))
        self.assertEqual(self.s.bookings, [OWN_BOOKING])
        self.checkpoint.assert_not_called()
        self.s.evidence.assert_not_awaited()

    async def ticks(self):
        """{control: ticked} for every Task List checkbox, as the page has them."""
        return await self.s.frame().evaluate(
            """() => Object.fromEntries([...document.querySelectorAll(
                '#GridViewTask input[type="checkbox"]')].map(
                    e => [e.id.split('_')[2], e.checked]))"""
        )

    async def box_gone(self):
        """The [x close] postback has been answered and the box is off the page."""
        frame = self.s.frame()
        await frame.locator("#popupDialog").wait_for(state="detached", timeout=5000)
        self.assertEqual(
            await frame.locator("#popupDialogPanel_sp").inner_html(), NO_BOX
        )

    async def show(self, markup):
        """Put this markup where FBS puts its box, without any postback."""
        await self.s.frame().evaluate(
            """markup => {
                document.getElementById('popupDialogPanel_sp').innerHTML = markup;
            }""",
            markup,
        )

    async def open_by_hand(self, ctl="ctl02"):
        """Tick the row and press Approve Selected, as a person does."""
        frame = self.s.frame()
        await frame.locator("#" + base.box_id(ctl)).check()
        await frame.locator("a#GridViewTask_ctl13").click()
        await frame.locator("#popupDialog div.dialog").wait_for(
            state="visible", timeout=10000
        )
        return frame

    async def approved(self, how, boxes=SINGLE, verification=None):
        """approve_task succeeds after one Approve Selected and one CONFIRM."""
        result = await approve_task(self.s.client, TASK)
        self.assert_posts(approvals=[boxes], confirms=[boxes])
        self.assertEqual(self.s.acted, [REFERENCE])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["reference_number"], REFERENCE)
        self.assertEqual(result["task"], TASK)
        self.assertEqual(result["task_status"], how)
        self.assertFalse(result["booking_status_verified"])
        self.assertEqual(
            (result["receipt_kind"], result["receipt_png"]), ("booking_details", PNG)
        )
        self.s.evidence.assert_awaited_once_with(self.s.client, REFERENCE)
        self.checkpoint.assert_called_once()
        verified = self.checkpoint.call_args.args[0]
        self.assertEqual(
            (verified["status"], verified["reference_number"], verified["task_status"]),
            ("approved", REFERENCE, how),
        )
        self.assertEqual(self.names(), [SUBMITTING, CONFIRMING, VERIFIED])
        if verification:
            self.assertEqual(self.events[-1][1]["verification"], verification)
        self.assertIn(base.SUBMITTING, self.progress())
        return result

    async def unknown(self, confirms=(SINGLE,), approvals=(SINGLE,)):
        """approve_task ends uncertain after exactly these posts; returns why."""
        with self.assertRaises(TaskApprovalOutcomeUnknown) as caught:
            await approve_task(self.s.client, TASK)
        self.assertIn("Check FBS Task List", str(caught.exception))
        self.assert_posts(approvals=approvals, confirms=confirms)
        self.checkpoint.assert_not_called()
        self.s.evidence.assert_not_awaited()
        self.assertEqual(
            self.names(), [SUBMITTING, *([CONFIRMING] if confirms else []), UNKNOWN]
        )
        return self.events[-1][1]["error_type"]

    async def refused(self, approvals=(SINGLE,)):
        """approve_task says nothing was submitted, and leaves the box closed."""
        with self.assertRaises(TaskUnavailable) as caught:
            await approve_task(self.s.client, TASK)
        self.assertIn("No approval was submitted", str(caught.exception))
        await self.box_gone()
        self.assert_posts(approvals=approvals, closes=1)
        self.assertFalse(self.s.box_open)
        self.assertEqual(self.names(), [SUBMITTING, NOT_SUBMITTED])
        return str(caught.exception)


# -- 0. The stand-in itself follows the observed flow -----------------------------


class ObservedFlowTests(BoxCase):
    async def test_the_stand_in_follows_the_flow_observed_on_21_nov_2026(self):
        frame = self.s.frame()
        self.assertEqual(frame.parent_frame.name, "frameBottom")
        self.assertEqual(urlsplit(frame.url).path, LIST_PATH)
        bar = frame.locator("#GridViewTask_actsonitems")
        panel = frame.locator("#popupDialogPanel_sp")
        loads = []
        self.s.page.on("framenavigated", lambda navigated: loads.append(navigated.url))

        async def labels(selector):
            return [
                " ".join(text.split())
                for text in await frame.locator(selector).all_inner_texts()
            ]

        # 1. Nothing ticked: no toolbar. Ticking the row shows it, and FBS's
        #    script ticks the header too.
        self.assertEqual(await self.ticks(), {"ctl01": False, "ctl02": False})
        self.assertFalse(await bar.is_visible())
        await frame.locator("#" + base.box_id("ctl02")).check()
        self.assertEqual(await self.ticks(), {"ctl01": True, "ctl02": True})
        self.assertTrue(await bar.is_visible())
        self.assertEqual(
            await labels("#GridViewTask_actsonitems a"),
            ["APPROVE SELECTED", "REJECT SELECTED"],
        )
        # 4. The message area is there, hidden.
        self.assertEqual(
            await frame.locator("#PanelMessage").evaluate("e => e.outerHTML"),
            MESSAGE_PANEL,
        )
        self.assertFalse(await frame.locator("#MessagePanel").is_visible())
        self.assertEqual(await panel.inner_html(), NO_BOX)

        # 2. Approve Selected approves nothing; the page comes back with both
        #    boxes rendered ticked, the toolbar visible, and the box.
        await frame.locator("a#GridViewTask_ctl13").click()
        await frame.locator("#popupDialog div.dialog").wait_for(state="visible")
        self.assertEqual(self.s.approvals, [SINGLE])
        self.assertEqual(await panel.inner_html(), BOX)
        self.assertEqual(
            await frame.locator(
                '#GridViewTask input[type="checkbox"][checked="checked"]'
            ).count(),
            2,
        )
        self.assertEqual(await self.ticks(), {"ctl01": True, "ctl02": True})
        self.assertTrue(await bar.is_visible())
        self.assertEqual(await labels("#popupDialog_l1"), ["Approve Selected"])
        self.assertEqual(await labels("#popupDialog_b1"), ["CONFIRM"])
        self.assertEqual(await labels("#popupDialog_bc"), ["[X CLOSE]"])
        self.assertEqual(await frame.locator("#textComments_c1").input_value(), "")
        self.assertEqual(self.s.tasks, [ROW])
        self.assertEqual(self.s.acted, [])

        # 3. [x close] removes the box; everything else stays as it was.
        await frame.locator("a#popupDialog_bc").click()
        await self.box_gone()
        self.assertEqual(self.s.closes, [SINGLE])
        self.assertEqual(await self.ticks(), {"ctl01": True, "ctl02": True})
        self.assertTrue(await bar.is_visible())
        # UpdatePanels: none of this reloaded the frame.
        self.assertEqual(loads, [])
        self.assertEqual((await self.s.read(self.s.client))["tasks"], [TASK])
        self.assert_posts(approvals=[SINGLE], closes=1)
        self.assert_untouched(ROW)

    async def test_my_bookings_lists_a_co_booking_only_once_accepted(self):
        # 5. Before: the pending co-booking is not in My Bookings.
        before = await self.s.read_bookings(self.s.client, reference=REFERENCE)
        self.assertEqual(
            before,
            {
                "bookings": [{**OWN_BOOKING, "can_cancel": False}],
                "total": 1,
                "complete": True,
            },
        )
        self.assertEqual(urlsplit(self.s.frame().url).path, BOOKINGS_PATH)
        self.s.bookings.append(booking_of(ROW, "Confirmed"))
        after = await self.s.read_bookings(self.s.client, reference=REFERENCE)
        self.assertEqual(
            after["bookings"][1],
            {
                "reference_number": REFERENCE,
                "date_time": ROW["date_time"],
                "room": ROW["room"],
                "fbs_status": "Confirmed",
                "can_cancel": False,
            },
        )
        self.assertEqual(self.s.unexpected, [])


# -- 1. approval_box and close_approval_box on the observed markup ----------------


class ApprovalBoxTests(BoxCase):
    async def test_returns_the_confirm_button_of_the_observed_box(self):
        self.assertIsNone(await approval_box(self.s.frame()))  # no box yet
        frame = await self.open_by_hand()
        button = await approval_box(frame)
        self.assertEqual(await button.count(), 1)
        self.assertEqual(await button.get_attribute("id"), "popupDialog_b1")
        self.assertEqual(
            await button.get_attribute("onclick"), base.postback(CONFIRM_TARGET)
        )
        # Looking presses nothing.
        self.assert_posts(approvals=[SINGLE])
        # Pressing it is the box's CONFIRM postback, with an empty comment.
        await button.click()
        await frame.locator("#popupDialog").wait_for(state="detached", timeout=5000)
        self.assert_posts(approvals=[SINGLE], confirms=[SINGLE])

    async def test_a_box_that_is_present_but_hidden_is_none(self):
        for name, markup in HIDDEN.items():
            with self.subTest(name):
                await self.show(markup)
                self.assertEqual(
                    await self.s.frame().locator("#popupDialog_b1").count(), 1
                )
                self.assertIsNone(await approval_box(self.s.frame()))
        self.assert_posts()

    async def test_any_other_box_is_not_recognised(self):
        for name, markup in UNRECOGNISED.items():
            with self.subTest(name):
                await self.show(markup)
                with self.assertRaises(LayoutChanged):
                    await approval_box(self.s.frame())
        self.assert_posts()

    async def test_a_confirm_that_asks_a_question_first_is_refused(self):
        for name, markup in ASKING.items():
            with self.subTest(name):
                await self.show(markup)
                with self.assertRaises(TaskUnavailable) as caught:
                    await approval_box(self.s.frame())
                self.assertIn("No approval was submitted", str(caught.exception))
        self.assert_posts()

    async def test_close_presses_x_close_once(self):
        frame = self.s.frame()
        await close_approval_box(frame)  # no box: nothing to press
        self.assert_posts()
        await self.open_by_hand()
        await close_approval_box(frame)
        await self.box_gone()
        self.assertIsNone(await approval_box(frame))
        self.assert_posts(approvals=[SINGLE], closes=1)
        self.assertEqual(self.s.closes, [SINGLE])
        self.assertEqual(self.events, [])
        self.assert_untouched(ROW)


# -- 2. The fallback proof on fresh listings --------------------------------------


class FallbackProofTests(BoxCase):
    """left_tasks_and_listed against fresh loads of the stand-in's two lists."""

    async def proof(self, *, tasks=(), bookings=(), hidden=0):
        self.s.tasks = [dict(task) for task in tasks]
        self.s.bookings = [dict(OWN_BOOKING), *bookings]
        self.s.hidden_items = hidden
        self.s.booking_reads.clear()
        return await left_tasks_and_listed(self.s.client, TASK)

    async def test_gone_from_task_list_and_listed_once_returns_the_status(self):
        for status in ("Confirmed", "Pending Confirmation", "Pending Approval"):
            with self.subTest(status):
                self.assertEqual(
                    await self.proof(bookings=[booking_of(ROW, status)]), status
                )
                self.assertEqual(self.s.booking_reads, [REFERENCE])
        # Another task still listed does not matter.
        self.assertEqual(
            await self.proof(tasks=[OTHER], bookings=[booking_of(ROW, "Confirmed")]),
            "Confirmed",
        )

    async def test_a_rejected_cancelled_or_withdrawn_listing_is_no_proof(self):
        for status in (
            "Rejected",
            "Withdrawn",
            "Cancelled",
            "Canceled",
            "Pending Cancellation",
        ):
            with self.subTest(status):
                self.assertIsNone(await self.proof(bookings=[booking_of(ROW, status)]))

    async def test_not_listed_or_listed_twice_is_no_proof(self):
        self.assertIsNone(await self.proof())
        self.assertIsNone(await self.proof(bookings=[booking_of(OTHER, "Confirmed")]))
        twice = [booking_of(ROW, "Confirmed"), booking_of(ROW, "Confirmed")]
        self.assertIsNone(await self.proof(bookings=twice))
        self.assertEqual(self.s.booking_reads, [REFERENCE])

    async def test_a_task_still_listed_is_no_proof_and_my_bookings_is_not_read(self):
        listed = [booking_of(ROW, "Confirmed")]
        self.assertIsNone(await self.proof(tasks=[ROW], bookings=listed))
        self.assertEqual(self.s.booking_reads, [])

    async def test_an_incomplete_task_list_is_no_proof(self):
        listed = [booking_of(ROW, "Confirmed")]
        self.assertIsNone(await self.proof(tasks=[OTHER], bookings=listed, hidden=1))
        self.assertEqual(self.s.listings[-1]["complete"], False)
        self.assertEqual(self.s.booking_reads, [])


# -- 3. Approved: one Approve Selected, one CONFIRM, then verified ----------------


class ApprovedTests(BoxCase):
    async def test_a_acknowledged_and_removed(self):
        loads = []
        self.s.page.on("framenavigated", lambda frame: loads.append(frame.url))
        result = await self.approved(
            ACKNOWLEDGED, verification="acknowledgement_and_removed_task"
        )
        self.assertEqual(result["task_status"], "Approval acknowledged by FBS")
        self.assertEqual(self.s.tasks, [])
        # Verified on FBS's own response: only approve_task's first read loaded
        # the Task List, and My Bookings was not needed.
        self.assertEqual(loads, [LIST_URL])
        self.assertEqual(self.s.reads, 1)
        self.assertEqual(self.s.booking_reads, [])

    async def test_b_removed_and_listed_confirmed_in_my_bookings(self):
        self.s.after_confirm = "listed"
        result = await self.approved(
            listed_as("Confirmed"),
            verification="left_task_list_and_listed_in_my_bookings",
        )
        self.assertEqual(
            result["task_status"], "Left the Task List; My Bookings shows Confirmed"
        )
        # One fresh Task List and one My Bookings read, for this reference.
        self.assertEqual(self.s.reads, 2)
        self.assertEqual(self.s.listings[-1]["tasks"], [])
        self.assertEqual(self.s.listings[-1]["complete"], True)
        self.assertEqual(self.s.booking_reads, [REFERENCE])

    async def test_c_removed_and_listed_pending_another_co_booker(self):
        self.s.after_confirm = "listed"
        self.s.listed_status = "Pending Confirmation"
        await self.approved(
            listed_as("Pending Confirmation"),
            verification="left_task_list_and_listed_in_my_bookings",
        )
        self.assertEqual(self.s.booking_reads, [REFERENCE])

    async def test_f_status_becomes_confirmed_in_place(self):
        self.s.after_confirm = "status"
        await self.approved("Confirmed", verification="task_status")
        self.assertEqual(self.s.tasks[0]["status"], "Confirmed")
        self.assertEqual(self.s.reads, 1)
        self.assertEqual(self.s.booking_reads, [])

    async def test_checkpointed_before_the_receipt_is_captured(self):
        self.s.after_confirm = "listed"

        async def capture(client, frame, outcome):
            self.checkpoint.assert_called_once()
            self.assertEqual(self.s.confirms, [SINGLE])

        with patch("sagasu.fbs_tasks.capture_approval_receipt", side_effect=capture):
            result = await approve_task(self.s.client, TASK)
        self.assertEqual(result["task_status"], listed_as("Confirmed"))
        self.assert_posts(approvals=[SINGLE], confirms=[SINGLE])

    async def test_a_previous_acknowledgement_is_dismissed_first(self):
        # FBS's own message area (a#linkClose "Dismiss"), still showing an
        # earlier acknowledgement when the Task List is opened.
        self.s.stale_message = "1 task approved successfully."
        self.s.after_confirm = "listed"
        await self.s.read(self.s.client)
        self.assertTrue(await self.s.frame().locator("#MessagePanel").is_visible())
        await self.approved(listed_as("Confirmed"))


# -- 4. Outcome unknown after exactly one CONFIRM ---------------------------------


class OutcomeUnknownTests(BoxCase):
    async def removed_and_listed(self, status):
        self.s.after_confirm = "listed"
        self.s.listed_status = status
        self.assertEqual(await self.unknown(), "LayoutChanged")
        self.assertEqual(self.s.acted, [REFERENCE])
        self.assertEqual(self.s.booking_reads, [REFERENCE])

    async def test_d_removed_but_not_in_my_bookings(self):
        self.s.after_confirm = "unlisted"
        self.assertEqual(await self.unknown(), "LayoutChanged")
        self.assertEqual(self.s.tasks, [])
        self.assertEqual(self.s.booking_reads, [REFERENCE])

    async def test_e_removed_and_listed_rejected(self):
        await self.removed_and_listed("Rejected")

    async def test_e_removed_and_listed_withdrawn(self):
        await self.removed_and_listed("Withdrawn")

    async def test_e_removed_and_listed_cancelled(self):
        await self.removed_and_listed("Cancelled")

    async def test_removed_and_listed_twice(self):
        self.s.after_confirm = "listed_twice"
        self.assertEqual(await self.unknown(), "LayoutChanged")
        self.assertEqual(self.s.booking_reads, [REFERENCE])

    async def test_g_nothing_changes(self):
        self.quick()
        self.s.after_confirm = "nothing"
        self.assertEqual(await self.unknown(), "LayoutChanged")
        # CONFIRM was pressed once in 60 passes, and never again.
        self.assertEqual(self.s.tasks, [ROW])
        self.assertEqual(await self.s.frame().locator("#popupDialog").count(), 0)
        self.assertEqual((self.s.reads, self.s.booking_reads), (1, []))

    async def test_h_box_stays_open(self):
        self.quick()
        self.s.after_confirm = "box_stays"
        self.assertEqual(await self.unknown(), "LayoutChanged")
        # The box is still there with its CONFIRM, and was pressed only once.
        self.assertEqual(await self.s.frame().locator("a#popupDialog_b1").count(), 1)
        self.assertEqual(self.s.tasks, [ROW])
        self.assertEqual((self.s.reads, self.s.booking_reads), (1, []))

    async def test_i_removed_but_the_task_list_read_is_incomplete(self):
        self.s.tasks = [dict(OTHER), dict(ROW)]
        self.s.after_confirm = "listed_incomplete"
        self.assertEqual(
            await self.unknown(confirms=[SECOND], approvals=[SECOND]), "LayoutChanged"
        )
        # The booking is listed Confirmed, but a task could be on a page the
        # read did not reach, so My Bookings was not even consulted.
        self.assertEqual(self.s.tasks, [OTHER])
        self.assertEqual(self.s.listings[-1]["complete"], False)
        self.assertEqual(self.s.booking_reads, [])


class AlreadyListedTests(BoxCase):
    """A task that is not a co-booking: the account approves a request whose
    booking My Bookings already lists (its own request, say)."""

    # Regression (fixed after review).
    async def test_a_booking_my_bookings_listed_all_along_is_no_proof(self):
        request = dict(ROW, request_type="Booking Approval", status="Pending Approval")
        self.s.tasks = [request]
        self.s.bookings.append(booking_of(request, "Pending Approval"))
        before = copy.deepcopy(self.s.bookings)
        self.s.after_confirm = "unlisted"  # the task leaves; My Bookings is unchanged
        (task,) = (await self.s.read(self.s.client))["tasks"]
        self.assertEqual(
            (task["can_approve"], task["can_confirm"], task["status"]),
            (True, False, "Pending Approval"),
        )
        try:
            await approve_task(self.s.client, task)
        except TaskApprovalOutcomeUnknown:
            return
        finally:
            # The scenario itself: one CONFIRM, and My Bookings as it was.
            self.assert_posts(approvals=[SINGLE], confirms=[SINGLE])
            self.assertEqual(self.s.bookings, before)
        self.fail("approved on a listing that was there before CONFIRM")


# -- 5. Nothing submitted: refused before CONFIRM, and the box closed -------------


class NothingSubmittedTests(BoxCase):
    async def test_a_confirm_carrying_fbs_s_penalty_prompt_is_not_pressed(self):
        self.s.box = guarded(ZERO_PENALTY)
        message = await self.refused()
        self.assertIn("asks a question before approving", message)
        self.assert_untouched(ROW)

    async def test_a_confirm_carrying_another_prompt_is_not_pressed(self):
        self.s.box = guarded("Approve the selected task(s)?")
        message = await self.refused()
        self.assertIn("asks a question before approving", message)
        self.assert_untouched(ROW)

    async def test_the_reviewed_row_no_longer_ticked_is_refused(self):
        self.s.on_open = "none_ticked"
        message = await self.refused()
        self.assertIn("selection changed before confirmation", message)
        self.assert_untouched(ROW)


class TwoRowNothingSubmittedTests(BoxCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The reviewed task is the second row (ctl03); OTHER is ctl02.
        self.s.tasks = [dict(OTHER), dict(ROW)]

    async def test_another_row_ticked_when_the_box_opens_is_refused(self):
        self.s.on_open = "all_ticked"
        message = await self.refused(approvals=[SECOND])
        self.assertIn("selection changed before confirmation", message)
        # The [x close] post carried what the page held; CONFIRM never did.
        self.assertEqual(self.s.closes, [["ctl01", "ctl02", "ctl03"]])
        self.assert_untouched(OTHER, ROW)

    async def test_another_row_ticked_while_the_box_is_open_is_refused(self):
        found = fbs_tasks.approval_box

        async def box_then_another_tick(frame):
            button = await found(frame)
            if button is not None:
                # A click, so FBS's grid script reacts (it ticks the header).
                await frame.evaluate(
                    f"() => document.getElementById('{base.box_id('ctl02')}').click()"
                )
            return button

        with patch("sagasu.fbs_tasks.approval_box", box_then_another_tick):
            message = await self.refused(approvals=[SECOND])
        self.assertIn("selection changed before confirmation", message)
        self.assertEqual(self.s.closes, [["ctl01", "ctl02", "ctl03"]])
        self.assert_untouched(OTHER, ROW)


# -- 6. A box Sagasu does not recognise: CONFIRM is never pressed -----------------


class UnrecognisedBoxTests(BoxCase):
    async def unrecognised(self, markup):
        self.s.box = markup
        self.assertEqual(await self.unknown(confirms=()), "LayoutChanged")
        self.assert_untouched(ROW)

    async def test_a_box_titled_reject_selected(self):
        await self.unrecognised(REJECT_TITLE)

    async def test_two_confirm_links(self):
        await self.unrecognised(TWO_CONFIRMS)

    async def test_a_confirm_labelled_submit(self):
        await self.unrecognised(SUBMIT_LABEL)

    async def test_two_boxes(self):
        await self.unrecognised(TWO_BOXES)

    # Regression (fixed after review).
    async def test_a_second_button_beside_confirm(self):
        self.quick()
        self.s.after_confirm = "nothing"
        self.s.box = SECOND_BUTTON
        with self.assertRaises((TaskUnavailable, TaskApprovalOutcomeUnknown)):
            await approve_task(self.s.client, TASK)
        self.assertEqual(self.s.approvals, [SINGLE])
        self.assertEqual(self.s.unexpected, [])
        self.assertEqual(self.s.confirms, [])


# -- 7. A two-row list ------------------------------------------------------------


class TwoRowTests(BoxCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.s.tasks = [dict(OTHER), dict(ROW)]

    async def test_only_the_reviewed_row_is_posted_with_confirm(self):
        self.s.after_confirm = "listed"
        await self.approved(listed_as("Confirmed"), boxes=SECOND)
        # The other task is still listed as it was, and not in My Bookings.
        self.assertEqual(self.s.tasks, [OTHER])
        self.assertEqual(self.s.listings[-1]["tasks"], [OTHER_TASK])
        self.assertEqual(
            [row["reference_number"] for row in self.s.bookings],
            [OWN_BOOKING["reference_number"], REFERENCE],
        )

    async def test_acknowledged_with_the_other_task_untouched(self):
        await self.approved(ACKNOWLEDGED, boxes=SECOND)
        self.assertEqual(self.s.tasks, [OTHER])
        self.assertEqual(self.s.booking_reads, [])


class MovedTickTests(BoxCase):
    """The list FBS sends back with the box is not the list that was ticked:
    a task arrived above, and the one tick stayed on its control (ASP.NET names
    a row's checkbox by position), so it now sits on a task nobody reviewed.
    Whether FBS re-binds ticks this way has not been observed."""

    async def test_the_scenario_one_tick_on_the_task_above(self):
        self.s.tasks = [dict(OTHER), dict(ROW)]
        self.s.on_open = "arrival"
        await self.s.read(self.s.client)
        frame = await self.open_by_hand("ctl03")
        self.assertEqual(self.s.approvals, [SECOND])
        self.assertEqual(self.s.tasks, [ARRIVAL, OTHER, ROW])
        self.assertEqual(
            await self.ticks(),
            {"ctl01": False, "ctl02": False, "ctl03": True, "ctl04": False},
        )
        self.assertEqual(self.s.rendered["ctl03"], OTHER["key"])
        # The count alone still looks right, and the box is the observed one.
        self.assertTrue(await only_one_task_selected(frame))
        self.assertIsNotNone(await approval_box(frame))
        self.assert_posts(approvals=[SECOND])

    async def moved(self, tasks, boxes):
        self.quick()
        self.s.tasks = tasks
        self.s.on_open = "arrival"
        with self.assertRaises((TaskUnavailable, TaskApprovalOutcomeUnknown)):
            await approve_task(self.s.client, TASK)
        self.assertEqual(self.s.approvals, [boxes])
        self.assertEqual(self.s.unexpected, [])
        # CONFIRM must not be pressed for a task that was never reviewed.
        self.assertEqual(self.s.confirms, [])
        self.assertEqual(self.s.acted, [])

    # Regression (fixed after review).
    async def test_a_tick_that_moved_to_another_task_is_not_confirmed(self):
        await self.moved([dict(ROW)], SINGLE)

    # Regression (fixed after review).
    async def test_a_tick_that_moved_on_a_two_row_list_is_not_confirmed(self):
        await self.moved([dict(OTHER), dict(ROW)], SECOND)


class PageLoadTests(BoxCase):
    """FBS answers CONFIRM with a whole page load (as an UpdatePanel's redirect
    would) instead of replacing its panels. Not observed either way."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.s.full_postbacks = [CONFIRM_TARGET]
        await self.s.read(self.s.client)

    async def test_the_scenario_confirm_loads_a_new_page(self):
        loads = []
        self.s.page.on("framenavigated", lambda frame: loads.append(frame.url))
        frame = await self.open_by_hand()
        self.assertEqual(loads, [])  # Approve Selected: in place, as observed
        await frame.locator("a#popupDialog_b1").click()
        await frame.locator("#MessagePanel").wait_for(state="visible", timeout=10000)
        self.assertEqual(loads, [LIST_URL])
        self.assertEqual(
            await frame.locator("#MessageLabel").inner_text(),
            "1 task approved successfully.",
        )
        self.assert_posts(approvals=[SINGLE], confirms=[SINGLE])
        self.assertEqual(self.s.tasks, [])

    # Regression (fixed after review).
    async def test_a_read_cut_short_by_the_page_load_after_confirm_is_retried(self):
        # Make "in flight" certain: once the page is leaving, the Task List
        # read stays pending until the new page replaces it.
        in_flight = (
            "() => window.STANDIN_LEAVING ? new Promise(() => {}) : ("
            + TASK_SNAPSHOT
            + ")()"
        )
        with patch("sagasu.fbs_tasks.TASK_SNAPSHOT", in_flight):
            await self.approved(ACKNOWLEDGED)


# -- 8. The same flow with FBSClient._wait_for_idle itself -------------------------


class RealIdleTests(BoxCase):
    """approve_task waiting as it does on FBS: FBSClient._wait_for_idle asks
    ASP.NET's page request manager and settles for 250 ms each time."""

    REAL_IDLE = True

    async def test_a_acknowledged_and_removed(self):
        await self.approved(
            ACKNOWLEDGED, verification="acknowledgement_and_removed_task"
        )

    async def test_b_removed_and_listed_confirmed_in_my_bookings(self):
        self.s.after_confirm = "listed"
        await self.approved(
            listed_as("Confirmed"),
            verification="left_task_list_and_listed_in_my_bookings",
        )

    async def test_another_row_ticked_when_the_box_opens_is_refused(self):
        self.s.tasks = [dict(OTHER), dict(ROW)]
        self.s.on_open = "all_ticked"
        await self.refused(approvals=[SECOND])
        self.assert_untouched(OTHER, ROW)


# -- 9. Through AgentService as a remote caller ------------------------------------


class RemoteServiceTests(BoxCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.backend = base.StandInBackend(self.s)
        self.service = AgentService(backend=self.backend)
        self.addAsyncCleanup(self.service.close)

    async def finish(self, job):
        await self.service.get(job["operation_id"]).task
        return self.service.get(job["operation_id"])

    async def review(self):
        listing = await self.finish(self.service.start("tasks"))
        self.assertEqual(listing.result["tasks"], [TASK])
        review = self.service.review_task_approval(listing.id, REFERENCE)
        self.assertTrue(
            review["summary"]["action"].startswith("Approve this task only"),
            review["summary"]["action"],
        )
        return review

    async def test_remote_caller_confirms_once_and_never_replays(self):
        self.s.after_confirm = "listed"
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context), remote_origin():
            review = await self.review()
            submitted = self.service.submit(review["review_id"])
            done = await self.finish(submitted)
            self.assertEqual(done.status, "succeeded", done.public())
            self.assertEqual(done.result["status"], "approved")
            self.assertEqual(done.result["task_status"], listed_as("Confirmed"))
            self.assertEqual(
                done.verified_result["task_status"], listed_as("Confirmed")
            )
            again = self.service.submit(review["review_id"])
            self.assertEqual(again["operation_id"], submitted["operation_id"])
            self.assertEqual(again["status"], "succeeded")
        self.assert_posts(approvals=[SINGLE], confirms=[SINGLE])
        self.assertEqual(self.backend.kinds.count("approve_task"), 1)
        self.assertEqual(done.receipt, PNG)
        self.assertEqual(self.s.contexts[0]["interactive"], False)
        self.assertEqual(self.names(), [SUBMITTING, CONFIRMING, VERIFIED])

    async def test_nothing_submitted_fails_the_job_and_a_fresh_review_is_allowed(self):
        self.s.box = guarded(ZERO_PENALTY)
        self.s.after_confirm = "listed"
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context), remote_origin():
            review = await self.review()
            submitted = self.service.submit(review["review_id"])
            failed = await self.finish(submitted)
            # "failed", not "outcome_unknown": nothing reached FBS's CONFIRM.
            self.assertEqual(failed.status, "failed", failed.public())
            self.assertEqual(failed.error["code"], "TaskUnavailable")
            self.assertFalse(failed.public()["reconciliation_required"])
            await self.box_gone()
            self.assert_posts(approvals=[SINGLE], closes=1)
            self.assert_untouched(ROW)
            # The same review still names the same, failed, operation.
            again = self.service.submit(review["review_id"])
            self.assertEqual(again["operation_id"], submitted["operation_id"])
            self.assertEqual(again["status"], "failed")
            self.assertEqual(self.backend.kinds.count("approve_task"), 1)
            # FBS's box is as observed again: a fresh review and submit go through.
            self.s.box = BOX
            fresh = await self.review()
            resubmitted = self.service.submit(fresh["review_id"])
            self.assertNotEqual(resubmitted["operation_id"], submitted["operation_id"])
            done = await self.finish(resubmitted)
            self.assertEqual(done.status, "succeeded", done.public())
            self.assertEqual(done.result["task_status"], listed_as("Confirmed"))
        self.assert_posts(approvals=[SINGLE, SINGLE], confirms=[SINGLE], closes=1)
        self.assertEqual(self.backend.kinds.count("approve_task"), 2)
        self.assertEqual(self.s.acted, [REFERENCE])

    async def test_an_unverified_confirm_is_unknown_and_blocks_a_fresh_submit(self):
        # The contrast: once CONFIRM is pressed, the job is never "failed".
        self.quick()
        self.s.after_confirm = "nothing"
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context), remote_origin():
            review = await self.review()
            done = await self.finish(self.service.submit(review["review_id"]))
            self.assertEqual(done.status, "outcome_unknown", done.public())
            self.assertEqual(done.error["code"], "TaskApprovalOutcomeUnknown")
            self.assertTrue(done.public()["reconciliation_required"])
            fresh = await self.review()
            with self.assertRaises(AgentError) as caught:
                self.service.submit(fresh["review_id"])
            self.assertIn("active or unresolved", str(caught.exception))
        self.assert_posts(approvals=[SINGLE], confirms=[SINGLE])
        self.assertEqual(self.backend.kinds.count("approve_task"), 1)


# -- 10. Through the MCP tools as a remote agent -----------------------------------


@unittest.skipIf(Client is None, "Install Sagasu for MCP runtime tests")
class RemoteAgentTests(BoxCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.backend = base.StandInBackend(self.s)
        self.service = AgentService(backend=self.backend)
        self.addAsyncCleanup(self.service.close)
        self.server = create_server(
            self.service, allow_agent_submission=True, remote=RemoteAccess(writes=True)
        )

    async def call(self, client, name, **arguments):
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result)
        return result.structured_content

    async def finish(self, client, started):
        while True:
            done = await self.call(
                client,
                "wait_operation",
                operation_id=started["operation_id"],
                timeout=30,
            )
            if done["terminal"]:
                return done

    async def submit(self, client):
        """list_tasks -> task_approval_review -> submit_review: (review, job)."""
        listing = await self.finish(client, await self.call(client, "list_tasks"))
        self.assertEqual(listing["status"], "succeeded")
        self.assertEqual(listing["result"]["tasks"], [TASK])
        review = await self.call(
            client,
            "task_approval_review",
            tasks_operation_id=listing["operation_id"],
            reference_number=REFERENCE,
        )
        self.assertTrue(
            review["summary"]["action"].startswith("Approve this task only")
        )
        submitted = await self.call(
            client, "submit_review", review_id=review["review_id"]
        )
        return review, submitted

    async def test_remote_agent_confirms_once_through_its_tools(self):
        self.s.after_confirm = "listed"
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context):
            async with Client(self.server) as client:
                review, submitted = await self.submit(client)
                done = await self.finish(client, submitted)
                self.assertEqual(done["status"], "succeeded", done)
                self.assertEqual(done["result"]["status"], "approved")
                self.assertEqual(done["result"]["task_status"], listed_as("Confirmed"))
                again = await self.call(
                    client, "submit_review", review_id=review["review_id"]
                )
                self.assertEqual(again["operation_id"], submitted["operation_id"])
        self.assert_posts(approvals=[SINGLE], confirms=[SINGLE])
        self.assertEqual(self.backend.kinds.count("approve_task"), 1)
        self.assertEqual(self.service.get(submitted["operation_id"]).receipt, PNG)
        self.assertEqual(self.s.contexts[0]["interactive"], False)

    async def test_nothing_submitted_fails_and_the_agent_may_review_again(self):
        self.s.on_open = "none_ticked"
        self.s.after_confirm = "listed"
        with patch("sagasu.fbs_tasks.FBSClient", self.s.context):
            async with Client(self.server) as client:
                review, submitted = await self.submit(client)
                failed = await self.finish(client, submitted)
                self.assertEqual(failed["status"], "failed", failed)
                self.assertEqual(failed["error"]["code"], "TaskUnavailable")
                self.assertFalse(failed["reconciliation_required"])
                await self.box_gone()
                self.assert_posts(approvals=[SINGLE], closes=1)
                self.assert_untouched(ROW)
                again = await self.call(
                    client, "submit_review", review_id=review["review_id"]
                )
                self.assertEqual(again["operation_id"], submitted["operation_id"])
                # FBS keeps the tick this time: a fresh review and submit work.
                self.s.on_open = None
                _, resubmitted = await self.submit(client)
                self.assertNotEqual(
                    resubmitted["operation_id"], submitted["operation_id"]
                )
                done = await self.finish(client, resubmitted)
                self.assertEqual(done["status"], "succeeded", done)
                self.assertEqual(done["result"]["task_status"], listed_as("Confirmed"))
        self.assert_posts(approvals=[SINGLE, SINGLE], confirms=[SINGLE], closes=1)
        self.assertEqual(self.backend.kinds.count("approve_task"), 2)
        self.assertEqual(self.s.acted, [REFERENCE])


if __name__ == "__main__":
    unittest.main()
