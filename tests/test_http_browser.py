"""Local HTTP/browser parity fixtures. No live FBS access or reservations."""

import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from sagasu.fbs_client import AuthenticationRequired
from sagasu.fbs_http import read_account_http
from sagasu.fbs_traffic import ReadTraffic
from sagasu.read_backends import HybridReads
from tests.test_http_reads import bookings_html, tasks_html


class BrowserHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state = self.state = {"status": "Confirmed", "requests": [], "posts": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                state["posts"] += 1
                self.send_error(405)

            def do_GET(self):
                state["requests"].append((self.path, self.headers.get("Cookie")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/should-not-follow")
                    self.end_headers()
                    return
                if self.path == "/shell":
                    html = '<iframe name="frameBottom" src="/nav"></iframe><iframe name="frameContent" src="/blank"></iframe>'
                elif self.path == "/nav":
                    html = '<div id="MyBookingsTab" onclick="top.frames.frameContent.location=\'/bookings\'">My Bookings</div><div id="TaskListTab" onclick="top.frames.frameContent.location=\'/tasks\'">Tasks</div>'
                elif self.path in {
                    "/bookings",
                    "/modules/homenavigation/mybookingstab.aspx",
                }:
                    html = bookings_html(status=state["status"])
                elif self.path in {"/tasks", "/modules/homenavigation/tasklist.aspx"}:
                    html = tasks_html() + '<input id="txtSearch_c1" value="">'
                else:
                    html = "<html><body>fixture</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addAsyncCleanup(self.stop_server)
        self.origin = f"http://127.0.0.1:{server.server_port}"
        for module in ["sagasu.fbs_http", "sagasu.fbs_traffic", "sagasu.fbs_api"]:
            p = patch(
                module + ".fbs_document_url",
                side_effect=lambda url: url.startswith(self.origin + "/"),
            )
            p.start()
            self.addCleanup(p.stop)
        p = patch("sagasu.fbs_api.FBS_URL", self.origin + "/home")
        p.start()
        self.addCleanup(p.stop)
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.context = await browser.new_context()
        await self.context.add_cookies(
            [{"name": "session", "value": "PRIVATE_FIXTURE", "url": self.origin}]
        )
        self.page = await self.context.new_page()
        await self.page.goto(self.origin + "/shell")
        self.client = SimpleNamespace(
            page=self.page,
            context=self.context,
            ensure_authenticated=AsyncMock(),
            progress=AsyncMock(),
            _wait_for_idle=AsyncMock(),
        )

    async def stop_server(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()

    async def test_real_browser_and_http_share_auth_and_preserve_fresh_status(self):
        router = HybridReads("auto")
        first = await router.verify_account(self.client, "bookings")
        self.assertEqual(first["read_backend"]["api_verification"], "verified")
        self.assertEqual(first["bookings"][0]["fbs_status"], "Confirmed")
        self.state["status"] = "Cancelled"
        second = await router.direct("bookings", request=self.client.context.request)
        self.assertEqual(second["read_backend"]["name"], "api")
        self.assertEqual(second["bookings"][0]["fbs_status"], "Cancelled")
        self.assertEqual(self.client.ensure_authenticated.await_count, 1)
        requests = [
            r
            for r in self.state["requests"]
            if r[0] in {"/bookings", "/modules/homenavigation/mybookingstab.aspx"}
        ]
        self.assertEqual(len(requests), 3)  # browser, parity GET, next read GET
        self.assertTrue(
            all("session=PRIVATE_FIXTURE" in cookie for _, cookie in requests)
        )
        self.assertEqual(self.state["posts"], 0)

    async def test_task_http_permissions_match_rendered_browser(self):
        router = HybridReads("auto")
        first = await router.verify_account(self.client, "tasks")
        self.assertEqual(first["read_backend"]["api_verification"], "verified")
        second = await router.direct("tasks", request=self.client.context.request)
        self.assertEqual(second["read_backend"]["name"], "api")
        self.assertTrue(second["tasks"][0]["can_approve"])
        self.assertTrue(second["tasks"][0]["can_view"])
        self.assertEqual(self.state["posts"], 0)

    async def test_real_redirect_is_not_followed_and_response_is_released(self):
        with self.assertRaises(AuthenticationRequired):
            await read_account_http(self.client, "bookings", self.origin + "/redirect")
        self.assertFalse(
            any(path == "/should-not-follow" for path, _ in self.state["requests"])
        )

    async def test_real_observer_only_uses_document_get_from_content_frame(self):
        with ReadTraffic(self.page) as traffic:
            frame = self.page.frame(name="frameContent")
            await frame.goto(self.origin + "/bookings")
            self.assertEqual(traffic.document_url(frame), self.origin + "/bookings")
        self.assertTrue(traffic.report()["requests"])
