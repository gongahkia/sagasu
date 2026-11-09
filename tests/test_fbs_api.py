"""Stateful local HTTP fixtures verify FBS request sequencing without Chromium."""

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from sagasu.fbs_api import FBSReadAPI
from sagasu.fbs_client import AuthenticationRequired, SearchConfig
from sagasu.fbs_http import HTTPReadUnavailable
from tests.test_http_reads import bookings_html, tasks_html
from tests.test_webforms import calendar_html


class APIProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state = self.state = {
            "version": 0,
            "date": "10-Nov-2099",
            "from": "10:00",
            "to": "12:00",
            "building": "",
            "posts": [],
            "invalid_state": False,
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/home":
                    self.send(200, '<iframe id="frameBottom" src="/nav"></iframe>')
                elif path == "/nav":
                    self.send(
                        200,
                        '<iframe id="frameContent" src="/modules/BigSpace/BookingCalendar/Calendar.aspx?TYPE=fixture"></iframe>',
                    )
                elif path.endswith("mybookingstab.aspx"):
                    self.send(200, bookings_html())
                elif path.endswith("tasklist.aspx"):
                    self.send(
                        200,
                        tasks_html()
                        + '<input id="txtSearch_c1" value="'
                        + state.get("task_filter", "")
                        + '">',
                    )
                elif path.endswith("Calendar.aspx"):
                    self.send(200, self.form())
                elif path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/should-not-follow")
                    self.end_headers()
                else:
                    self.send(404, "unknown")

            def form(self, model=False):
                value = lambda name: state.get(name, "")
                html = (
                    '<form><input name="__PageUniqueId__" value="READ-PAGE"><input name="__ViewStateKey__" value="'
                    + str(state["version"])
                    + '"><input name="DateBookingFrom_c1$textDate" value="'
                    + state["date"]
                    + '">'
                )
                for label, key in [("TimeFrom_c1", "from"), ("TimeTo_c1", "to")]:
                    options = "".join(
                        '<option value="'
                        + t
                        + '"'
                        + (" selected" if t == state[key] else "")
                        + ">"
                        + t
                        + "</option>"
                        for t in ["10:00", "11:00", "12:00", "13:00"]
                    )
                    html += (
                        '<select id="'
                        + label
                        + '_ctl04" name="'
                        + label
                        + '$ctl04">'
                        + options
                        + "</select>"
                    )
                filters = [
                    ("DropMultiBuildingList_c1", "Building A", "9"),
                    (
                        "DropMultiFloorList_c1",
                        "Level 2",
                        "7" if state.get("DropMultiBuildingList_c1_textValue") else "3",
                    ),
                    ("DropMultiFacilityTypeList_c1", "Group Study Room", "1"),
                    ("DropMultiEquipmentList_c1", "Projector", "4"),
                ]
                for control, label, idx in filters:
                    html += (
                        '<input name="'
                        + control
                        + '_textValue" value="'
                        + value(control + "_textValue")
                        + '"><div id="'
                        + control
                        + '::ddl::"><label><input type="checkbox" id="'
                        + idx
                        + '">'
                        + label
                        + "</label></div>"
                    )
                html += '<select id="DropCapacity_c1" name="DropCapacity_c1"><option value="">All</option></select><table id="GridResults_gv"><tr><td><input type="checkbox" name="GridResults$gv$ctl02$checkMultiple"></td><td>Room A</td></tr></table></form>'
                if model:
                    day = datetime.strptime(state["date"], "%d-%b-%Y").strftime(
                        "%Y-%m-%d"
                    )
                    html += calendar_html(
                        overrides={
                            "startDate": day + "T00:00:00",
                            "timeline": [
                                {"start": day + "T00:00:00", "end": day + "T23:59:00"}
                            ],
                        }
                    )
                return html

            def do_POST(self):
                fields = {
                    k: v[0]
                    for k, v in parse_qs(
                        self.rfile.read(int(self.headers["Content-Length"])).decode(),
                        keep_blank_values=True,
                    ).items()
                }
                if fields.get("__ViewStateKey__") != str(state["version"]):
                    state["invalid_state"] = True
                    self.send(409, "stale state")
                    return
                target = fields["__EVENTTARGET"]
                state["posts"].append(target)
                self.assert_read_target = target in {
                    "TimeFrom_c1$ctl04",
                    "TimeTo_c1$ctl04",
                    "BtnDpcNext$BtnDpcNext_bt",
                    "BtnDpcPrev$BtnDpcPrev_bt",
                    "CheckAvailability$CheckAvailability_bt",
                } or target.startswith("DropMulti")
                if not self.assert_read_target:
                    self.send(405, "write forbidden")
                    return
                state["version"] += 1
                if target.startswith("BtnDpc"):
                    date = datetime.strptime(state["date"], "%d-%b-%Y") + timedelta(
                        days=1 if "Next" in target else -1
                    )
                    state["date"] = date.strftime("%d-%b-%Y")
                for key in fields:
                    if key.startswith("DropMulti"):
                        state[key] = fields[key]
                state["from"] = fields["TimeFrom_c1$ctl04"]
                state["to"] = fields["TimeTo_c1$ctl04"]
                if (
                    target == "CheckAvailability$CheckAvailability_bt"
                    and fields.get("GridResults$gv$ctl02$checkMultiple") != "on"
                ):
                    self.send(400, "missing room")
                    return
                self.send(
                    200,
                    self.form(model=target == "CheckAvailability$CheckAvailability_bt"),
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addAsyncCleanup(self.stop_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        p = patch("sagasu.fbs_api.FBS_URL", self.origin + "/home")
        p.start()
        self.addCleanup(p.stop)
        p = patch(
            "sagasu.fbs_api.fbs_document_url",
            side_effect=lambda u: u.startswith(self.origin + "/"),
        )
        p.start()
        self.addCleanup(p.stop)
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        request = await driver.request.new_context()
        self.addAsyncCleanup(request.dispose)
        self.api = FBSReadAPI(request=request)

    async def stop_server(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()

    async def test_search_refreshes_state_and_resolves_dependent_filter_values(self):
        config = SearchConfig(
            "12-Nov-2099",
            "10:00",
            "12:00",
            buildings=("Building A",),
            floors=("Level 2",),
            facility_types=("Group Study Room",),
            equipment=("Projector",),
        )
        result = await self.api.search(config)
        self.assertEqual(len(result.bookable_rooms), 1)
        self.assertEqual(self.state["DropMultiFloorList_c1_textValue"], "7")
        self.assertEqual(self.state["posts"].count("BtnDpcNext$BtnDpcNext_bt"), 2)
        self.assertEqual(
            self.state["posts"][-1], "CheckAvailability$CheckAvailability_bt"
        )
        self.assertFalse(self.state["invalid_state"])

    async def test_account_reads_issue_no_postbacks(self):
        for kind in ["bookings", "tasks"]:
            result = await self.api.account(kind)
            self.assertEqual(result["total"], 1)
        self.assertEqual(self.state["posts"], [])
        with self.assertRaises(HTTPReadUnavailable):
            await self.api.account("book")

    async def test_inherited_task_filter_never_hides_pending_tasks_silently(self):
        self.state["task_filter"] = "filtered fixture"
        with self.assertRaises(HTTPReadUnavailable) as caught:
            await self.api.account("tasks")
        self.assertEqual(caught.exception.code, "filtered_account_listing")

    async def test_transport_rejects_writes_even_if_a_caller_bypasses_form_builder(
        self,
    ):
        for target in ["Confirm$Confirm_bt", "Cancel$Cancel_bt", "ApproveSelected"]:
            with self.assertRaises(HTTPReadUnavailable):
                await self.api.document(
                    self.origin + "/modules/BigSpace/BookingCalendar/Calendar.aspx",
                    {"__EVENTTARGET": target},
                )
        with self.assertRaises(HTTPReadUnavailable):
            await self.api.document(
                self.origin + "/modules/homenavigation/tasklist.aspx",
                {"__EVENTTARGET": "TimeFrom_c1$ctl04"},
            )
        self.assertEqual(self.state["posts"], [])

    async def test_redirect_is_rejected_without_following_or_exposing_destination(self):
        with self.assertRaises(AuthenticationRequired):
            await self.api.document(self.origin + "/redirect")


class APISessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_session_loads_into_http_context_without_launching_browser(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            cookie = {
                "domain": "fbs.intranet.smu.edu.sg",
                "name": "SESSION",
                "value": "PRIVATE_COOKIE",
                "path": "/",
            }
            (Path(directory) / ".sagasu-session.json").write_text(
                json.dumps([cookie, {**cookie, "domain": "login.microsoftonline.com"}])
            )
            request = Mock(dispose=AsyncMock())
            driver = Mock(
                request=SimpleNamespace(new_context=AsyncMock(return_value=request)),
                stop=AsyncMock(),
            )
            launcher = Mock(start=AsyncMock(return_value=driver))
            with patch("sagasu.fbs_api.async_playwright", return_value=launcher):
                async with FBSReadAPI(profile_path=directory) as api:
                    self.assertIs(api.request, request)
            driver.chromium.launch.assert_not_called()
            self.assertEqual(
                driver.request.new_context.await_args.kwargs["storage_state"][
                    "cookies"
                ],
                [cookie],
            )
            request.dispose.assert_awaited_once()
            driver.stop.assert_awaited_once()

    async def test_missing_or_malformed_session_requests_login_without_browser(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("sagasu.fbs_api.async_playwright") as driver,
        ):
            for contents in [None, "not json", "{}", "[]"]:
                if contents is not None:
                    (Path(directory) / ".sagasu-session.json").write_text(contents)
                with self.assertRaises(AuthenticationRequired):
                    async with FBSReadAPI(profile_path=directory):
                        pass
            driver.assert_not_called()
