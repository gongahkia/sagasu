"""Native-shaped EDIT fixture: references exist only in the source booking grid."""

import copy
import html
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from sagasu.fbs_client import FBSClient, FBS_URL

EDIT_URL = (
    "https://fbs.intranet.smu.edu.sg/modules/BigSpace/BookingManagement/edit.aspx"
)

SOURCE = {
    "reference_number": "BK-TEST",
    "room": "Room A",
    "date": "01-Nov-2099",
    "start_time": "14:00",
    "end_time": "16:00",
    "purpose": "Original purpose",
    "usage_type": "AdHoc",
    "booking_usage": "Meeting",
    "participants": ["FRIEND"],
    "building": "Li Ka Shing Library",
    "facility_type": "Project Room (Level 5)",
    "fbs_status": "Confirmed",
}


class AmendmentFixture:
    async def setup(self, case):
        self.saved = copy.deepcopy(SOURCE)
        self.complete, self.offered, self.drop_response, self.lose_participant = (
            True,
            True,
            False,
            False,
        )
        self.extra, self.form_overrides, self.actions, self.posts = [], {}, [], 0
        driver = await async_playwright().start()
        case.addAsyncCleanup(driver.stop)
        self.browser = await driver.chromium.launch(headless=True)
        case.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.expose_function(
            "record", lambda value: self.actions.append(value)
        )
        self.client = FBSClient(progress=AsyncMock())
        self.client.page = self.page
        await self.page.route("**/*", self.route)
        await self.page.goto(FBS_URL)
        for name in ["sagasu.amendments.FBSClient", "sagasu.mcp_service.FBSClient"]:
            mocked = patch(name, self.context)
            mocked.start()
            case.addCleanup(mocked.stop)
        for name in [
            "sagasu.amendments.read_bookings",
            "sagasu.reconciliation.read_bookings",
        ]:
            mocked = patch(name, self.read_bookings)
            mocked.start()
            case.addCleanup(mocked.stop)
        mocked = patch("sagasu.booking_details.read_booking_details", self.details)
        mocked.start()
        case.addCleanup(mocked.stop)

    @asynccontextmanager
    async def context(self, **kwargs):
        yield self.client

    def row(self, source):
        return {
            "reference_number": source["reference_number"],
            "room": source["room"],
            "date_time": f'{source["date"]} (Sun) {source["start_time"]} - {source["end_time"]} (2hrs)',
            "fbs_status": source["fbs_status"],
            "can_cancel": True,
        }

    async def read_bookings(self, client, **kwargs):
        iframe = self.page.locator('iframe[name="frameContent"]')
        await iframe.wait_for(state="attached")
        handle = await iframe.element_handle()
        frame = await handle.content_frame()
        await frame.goto("https://fbs.intranet.smu.edu.sg/list")
        rows = [self.row(self.saved), *copy.deepcopy(self.extra)]
        return {
            "bookings": rows,
            "total": len(rows) + (0 if self.complete else 5),
            "complete": self.complete,
        }

    async def details(self, client, reference, **kwargs):
        return {
            **copy.deepcopy(self.saved),
            "reference_verified": True,
            "additional_details": {
                "use_type": self.saved["usage_type"],
                "booking_usage": self.saved["booking_usage"],
            },
        }

    async def route(self, route):
        if route.request.method == "POST":
            self.posts += 1
            self.saved.update(json.loads(route.request.post_data))
            self.saved["fbs_status"] = "Pending Confirmation"
            if self.drop_response:
                await route.abort()
                return
            await route.fulfill(body="ok", content_type="text/plain")
            return
        if route.request.url == FBS_URL:
            body = '<iframe name="frameContent" src="/list"></iframe>'
        elif route.request.url.endswith("/list"):
            rows = [self.row(self.saved), *self.extra]
            body = '<div id="GridViewBooking_content"><table><tbody>'
            for index, row in enumerate(rows):
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
                body += '<tr class="row">' + "".join(
                    "<td>" + html.escape(value) + "</td>" for value in values
                )
                if self.offered and row["fbs_status"] == "Confirmed":
                    body += f'<td><a id="row{index}_BtnEdit" href="{EDIT_URL}" onclick="record(\'EDIT\')">EDIT</a></td>'
                body += "</tr>"
            body += "</tbody></table></div>"
        else:
            source = {**self.saved, **self.form_overrides}
            body = self.form(source)
        await route.fulfill(body=body, content_type="text/html")

    def form(self, source):
        fields = "".join(
            f'<span id="bookingFormControl1_{control}_c1">{html.escape(source[key])}</span>'
            for key, control in (
                ("room", "LabelFacilityName"),
                ("building", "LabelBuilding"),
                ("facility_type", "LabelPassType"),
            )
        )
        serialized = json.dumps(source).replace("</", "<\\/")
        return (
            fields
            + r"""
        <input id="bookingFormControl1_BookingDate_c1_textDate" readonly onclick="showCalendar()">
        <select id="bookingFormControl1_DropDownStartTime_c1" onchange="startMinute=Number(this.selectedOptions[0].dataset.minute);endMinute=startMinute+30;renderTimes()"></select>
        <select id="bookingFormControl1_DropDownEndTime_c1" onchange="endMinute=Number(this.selectedOptions[0].dataset.minute);estimate()"></select>
        <input id="bookingFormControl1_TextboxPurpose_c1">
        <select id="bookingFormControl1_DropDownUsageType_c1"><option value=""></option><option value="AdHoc">AdHoc</option><option value="Academic">Academic</option></select>
        <select id="bookingFormControl1_DropDownSpaceBookingUsage_c1" onchange="maybeLoseParticipant()"><option value=""></option><option value="Meeting">Meeting</option><option value="Research">Research</option></select>
        <span id="bookingFormControl1_BookingCreditsDeducted_Label_c1"></span>
        <div id="bookingFormControl1_GridCoBookers"></div>
        <input id="bookingFormControl1_TermsAndConditionsCheckbox_c1" type="checkbox" disabled checked>
        <div id="__panelCalendar__" hidden><div id="__calendar_title"></div><div id="__calendar_prevArrow" onclick="moveMonth(-1)">Previous</div><div id="__calendar_nextArrow" onclick="moveMonth(1)">Next</div><table id="__calendar_daysTable"></table></div>
        <a id="panel_UIButton2" href="#" onclick="submitEdit();return false">CONFIRM</a>
        <script>
        const original=__SOURCE__;
        const short=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const full=['January','February','March','April','May','June','July','August','September','October','November','December'];
        const $=suffix=>document.getElementById('bookingFormControl1_'+suffix);
        const number=t=>Number(t.slice(0,2))*60+Number(t.slice(3,5));
        const hh=n=>String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');
        let selectedDate=original.date, startMinute=number(original.start_time),endMinute=number(original.end_time),year,month;
        function dateParts(){const p=selectedDate.split('-');return [Number(p[2]),short.indexOf(p[1]),Number(p[0])];}
        function timeValue(n){const [y,m,d]=dateParts();return `${m+1}/${d}/${y} ${Math.floor(n/60)%12||12}:${String(n%60).padStart(2,'0')}:00 ${n<720?'AM':'PM'}`;}
        function estimate(){$('BookingCreditsDeducted_Label_c1').innerText=((endMinute-startMinute)/60*14).toFixed(2);}
        function renderTimes(){
            $('DropDownStartTime_c1').innerHTML=Array.from({length:32},(_,i)=>480+i*30).map(n=>`<option data-minute="${n}" value="${timeValue(n)}" ${n===startMinute?'selected':''}>${hh(n)}</option>`).join('');
            const ends=Array.from({length:32},(_,i)=>480+i*30).filter(n=>n>startMinute);
            $('DropDownEndTime_c1').innerHTML=ends.map(n=>`<option data-minute="${n}" value="${timeValue(n)}" ${n===endMinute?'selected':''}>${hh(n)}</option>`).join('');
            estimate();
        }
        function participants(names){$('GridCoBookers').innerHTML='<table><tr><th></th><th>NAME</th></tr></table><table>'+names.map(n=>`<tr class="row"><td><input type="checkbox"></td><td>${n}</td></tr>`).join('')+'</table>'+names.length+' ITEM(S) TOTAL';}
        function maybeLoseParticipant(){if(__LOSE__)participants([]);}
        function renderCalendar(){
            document.getElementById('__calendar_title').innerText=full[month].toUpperCase()+', '+year;
            document.getElementById('__calendar_daysTable').innerHTML=Array.from({length:new Date(year,month+1,0).getDate()},(_,i)=>`<tr><td><div class="day" title="${String(i+1).padStart(2,'0')}-${short[month]}-${year}" onclick="chooseDay(${i+1})">${i+1}</div></td></tr>`).join('');
        }
        function showCalendar(){[year,month]=dateParts();document.getElementById('__panelCalendar__').hidden=false;renderCalendar();}
        function moveMonth(delta){const d=new Date(year,month+delta,1);year=d.getFullYear();month=d.getMonth();renderCalendar();}
        function chooseDay(day){selectedDate=String(day).padStart(2,'0')+'-'+short[month]+'-'+year;$('BookingDate_c1_textDate').value=selectedDate;document.getElementById('__panelCalendar__').hidden=true;renderTimes();record('DATE');}
        function submitEdit(){record('CONFIRM');fetch(location.pathname,{method:'POST',body:JSON.stringify({date:selectedDate,start_time:hh(startMinute),end_time:hh(endMinute),purpose:$('TextboxPurpose_c1').value,usage_type:$('DropDownUsageType_c1').value,booking_usage:$('DropDownSpaceBookingUsage_c1').value})}).catch(()=>{});}
        $('BookingDate_c1_textDate').value=selectedDate;$('TextboxPurpose_c1').value=original.purpose;$('DropDownUsageType_c1').value=original.usage_type;$('DropDownSpaceBookingUsage_c1').value=original.booking_usage;
        renderTimes();participants(original.participants);
        </script>""".replace(
                "__SOURCE__", serialized
            ).replace(
                "__LOSE__", str(self.lose_participant).lower()
            )
        )
