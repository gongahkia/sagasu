"""Stand-in for FBS's cancel flow, shaped like the pages FBS served on 20 Nov 2026.

CANCEL in My Bookings posts the grid back, and FBS redirects to the booking form
in cancel mode (BookingManagement/edit.aspx). That form needs "Reason for
Cancelling" (bookingFormControl1_Remarks_c1), and its "Confirm Cancellation"
button (panel_UIButton2, outside #FullPanel) asks a browser confirm() about the
cancellation penalty before posting. Only the stand-in's routes are reached.
"""

import html
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

from playwright.async_api import async_playwright
from sagasu.fbs_client import FBSClient, FBS_URL

BASE = "https://fbs.intranet.smu.edu.sg"
LIST_URL = BASE + "/modules/homenavigation/mybookingstab.aspx"
EDIT_URL = BASE + "/modules/BigSpace/BookingManagement/edit.aspx?ID=%23test&TYPE=%23test"
PROMPT = (
    "A Cancellation Penalty of [{penalty}] will be imposed. "
    "Please click Ok if you wish to proceed"
)
ROW = {
    "reference_number": "BK-20990101-000001",
    "room": "Room A",
    "date_time": "01-Jan-2099 (Thu) 15:00 - 17:00 (2hrs)",
    "fbs_status": "Confirmed",
    "can_cancel": True,
}

SCRIPT = """<script>
var btn = {cl: function () {}, kp: function () { return true; }};
function v() {}
function __doPostBack(target, argument) {
  var form = document.forms[0];
  form.__EVENTTARGET.value = target;
  form.__EVENTARGUMENT.value = argument;
  form.submit();
}
</script>"""


def _form(action, body):
    return (
        '<html><head><meta charset="utf-8"><title>SMU FBS</title>'
        + SCRIPT
        + f'</head><body><form method="post" action="{html.escape(action)}">'
        + '<input type="hidden" name="__EVENTTARGET"><input type="hidden" name="__EVENTARGUMENT">'
        + body
        + "</form></body></html>"
    )


class CancellationFixture:
    """Knobs: penalty; prompt (the confirm() text, or "none" for no prompt);
    form_reference; open_form; drop_post; status_after; label."""

    async def setup(self, case):
        self.status = ROW["fbs_status"]
        self.penalty = "0.00"
        self.prompt = None
        self.form_reference = None
        self.open_form = True
        self.drop_post = False
        self.status_after = "Cancelled"
        self.label = "Confirm Cancellation"
        self.posts, self.reasons, self.opened = [], [], 0
        driver = await async_playwright().start()
        case.addAsyncCleanup(driver.stop)
        # A dead proxy: a request the routes below do not answer fails here
        # instead of reaching FBS or Microsoft.
        self.browser = await driver.chromium.launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"}
        )
        case.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.client = FBSClient(progress=AsyncMock())
        self.client.page = self.page
        await self.page.route("**/*", self.route)
        await self.page.goto(FBS_URL)
        for name in ("sagasu.fbs_account.read_bookings",):
            mocked = patch(name, self.read_bookings)
            mocked.start()
            case.addCleanup(mocked.stop)

    @asynccontextmanager
    async def context(self, **kwargs):
        yield self.client

    def row(self):
        return {**ROW, "fbs_status": self.status}

    def frame(self):
        return self.page.frame(name="frameContent")

    async def read_bookings(self, client, **kwargs):
        await self.frame().goto(LIST_URL)
        rows = [self.row()]
        return {"bookings": rows, "total": len(rows), "complete": True}

    def listing(self):
        row = self.row()
        values = [
            "",
            row["reference_number"],
            row["date_time"],
            "",
            row["room"],
            "",
            "",
            "",
            row["fbs_status"],
        ]
        cells = "".join(f"<td>{html.escape(value)}</td>" for value in values)
        if "cancel" not in self.status.casefold():
            cells += (
                '<td><a id="GridViewBooking_gv_ctl04_BtnCancel" '
                "onclick=\"btn.cl(this);setTimeout('__doPostBack(\\'GridViewBooking$gv$ctl04$BtnCancel$BtnCancel_bt\\',\\'\\')', 0);return false;\" "
                'onkeypress="return btn.kp();" href="javascript:v();" title="" class="hlink-blue"><span>Cancel</span></a></td>'
            )
        return _form(
            LIST_URL,
            '<div id="GridViewBooking"><div id="GridViewBooking_content"><table><tbody>'
            f'<tr class="row">{cells}</tr></tbody></table></div>1 ITEM(S) TOTAL</div>',
        )

    def cancel_form(self):
        prompt = self.prompt or PROMPT.format(penalty=self.penalty)
        guard = f"if(!confirm('{prompt}'))return false;" if prompt != "none" else ""
        button = (
            f'<a id="panel_UIButton2" onclick="{html.escape(guard)}btn.cl(this);'
            "setTimeout('__doPostBack(\\'panel$UIButton2$UIButton2_bt\\',\\'\\')', 0);return false;\" "
            'onkeypress="return btn.kp();" href="javascript:v();" title="" class="btnblack">'
            f"<span>{html.escape(self.label)}</span></a>"
        )
        reference = self.form_reference or ROW["reference_number"]
        return _form(
            EDIT_URL,
            '<div id="panel_ButtonContainerPanel"><span id="panel_spanSaveButtons">'
            + button
            + '<a id="panel_buttonReturn" onclick="return false;" href="javascript:v();" class="btn"><span>&nbsp;Back</span></a>'
            + "</span></div>"
            + '<div id="panel_buttonShadow"><a id="panel_UIButton2" style="display:none" href="javascript:v();"><span>Confirm Cancellation</span></a></div>'
            + '<div id="FullPanel">'
            + '<div class="field full" id="bookingFormControl1_Remarks"><div class="required table">'
            + '<div class="caption side left" style="width:200px;"><span>Reason for Cancelling*:</span></div>'
            + '<div class="control"><span><input name="bookingFormControl1$Remarks_c1" type="text" maxlength="255" id="bookingFormControl1_Remarks_c1" class="textbox" /></span></div>'
            + "</div></div>"
            + '<div class="field full" id="bookingFormControl1_ObjectNumber"><div class="table">'
            + '<div class="caption side left" style="width:200px;"><span>Ref Number:</span></div>'
            + f'<div class="control"><span id="bookingFormControl1_ObjectNumber_c1" class="label" style=" width:200px;">{html.escape(reference)}</span></div>'
            + "</div></div>"
            + '<input disabled="disabled" id="bookingFormControl1_TermsAndConditionsCheckbox_c1" type="checkbox" checked="checked" />'
            + "</div>",
        )

    @staticmethod
    def redirect(url):
        # Not a 3xx: Playwright does not route the request a fulfilled
        # redirect leads to, so it would leave the stand-in.
        return f"<script>location.replace({url!r})</script>"

    async def route(self, route):
        request = route.request
        url = request.url
        if request.method == "POST":
            fields = parse_qs(request.post_data or "")
            target = (fields.get("__EVENTTARGET") or [""])[0]
            if url.startswith(LIST_URL) and target.endswith("BtnCancel_bt"):
                self.opened += 1
                if not self.open_form:
                    await route.fulfill(body=self.listing(), content_type="text/html")
                    return
                await route.fulfill(body=self.redirect(EDIT_URL), content_type="text/html")
                return
            if url.startswith(EDIT_URL.split("?")[0]) and target == "panel$UIButton2$UIButton2_bt":
                reason = (fields.get("bookingFormControl1$Remarks_c1") or [""])[0]
                self.posts.append(target)
                self.reasons.append(reason)
                if self.drop_post:
                    await route.abort()
                    return
                if reason.strip():
                    self.status = self.status_after
                await route.fulfill(body=self.redirect(LIST_URL), content_type="text/html")
                return
            await route.fulfill(status=400, body="unexpected post")
            return
        if url == FBS_URL:
            body = f'<iframe name="frameContent" src="{LIST_URL}"></iframe>'
        elif url.startswith(LIST_URL):
            body = self.listing()
        elif url.split("?")[0].casefold() == EDIT_URL.split("?")[0].casefold():
            body = self.cancel_form()
        else:
            await route.fulfill(status=404, body="")
            return
        await route.fulfill(body=body, content_type="text/html")
