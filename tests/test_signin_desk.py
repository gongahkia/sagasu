"""The SMU sign-in page: a person with a key signs in on a live picture of
Microsoft's page, in this copy's browser.

The routes are called in-process. The live-picture test drives a real headless
Chromium on a local page that stands in for Microsoft's; it can reach nothing
else. Never contacts FBS, Microsoft or any outside service.
"""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from starlette.applications import Starlette

from sagasu import errors, live_view, signin_desk
from sagasu.fbs_client import AuthenticationRequired, FBSClient
from sagasu.mcp_service import AgentService
from sagasu.origin import is_remote
from sagasu.signin_desk import COOKIE, SignInDesk, is_desk_path
from tests.test_remote import DIGEST, KEY, OriginBackend

HOST = "sagasu.test"
ORIGIN = {"origin": f"https://{HOST}"}

STAND_IN = """<!doctype html><html><body style="margin:0">
<form id="form" style="padding:40px" onsubmit="event.preventDefault(); document.title = 'sent:' + email.value">
<input id="email" style="width:300px;height:40px;font-size:20px">
<button id="next" style="width:120px;height:40px">Next</button>
</form>
<a id="away" href="#" onclick="window.open('about:blank'); return false">Terms</a>
</body></html>"""


class GatedBackend(OriginBackend):
    """A login that waits until the test lets it finish."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()


async def call(app, method, path, *, headers=None, body=b"", cookie=None):
    path, _, query = path.partition("?")
    sent_headers = {"host": HOST, **(headers or {})}
    if cookie:
        sent_headers["cookie"] = f"{COOKIE}={cookie}"
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        sent_headers.setdefault("content-type", "application/json")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in sent_headers.items()],
        "client": ("192.0.2.1", 50000),
        "server": (HOST, 80),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    replies = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        replies.append(message)

    await app(scope, receive, send)
    start = replies[0]
    headers = {}
    for name, value in start["headers"]:
        headers.setdefault(name.decode().lower(), value.decode())
    return (
        start["status"],
        headers,
        b"".join(message.get("body", b"") for message in replies[1:]),
    )


class DeskCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = GatedBackend()
        self.service = AgentService(backend=self.backend)
        self.desk = SignInDesk(self.service, {DIGEST: "laptop"}, f"https://{HOST}")
        self.app = Starlette(routes=self.desk.routes())
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        self.backend.gate.set()
        await self.desk.close()
        await asyncio.gather(
            *(job.task for job in self.service.jobs.values() if job.task),
            return_exceptions=True,
        )

    async def enter(self, key=KEY, headers=None):
        status, response, body = await call(
            self.app,
            "POST",
            "/sign-in",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                **ORIGIN,
                **(headers or {}),
            },
            body=urlencode({"key": key}).encode(),
        )
        return status, response, body

    async def visit(self):
        status, headers, _ = await self.enter()
        self.assertEqual(status, 303)
        return headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    async def status(self, cookie):
        status, _, body = await call(self.app, "GET", "/sign-in/status", cookie=cookie)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    async def until(self, cookie, phase):
        for _ in range(200):
            state = await self.status(cookie)
            if state["phase"] == phase:
                return state
            await asyncio.sleep(0.01)
        self.fail(f"never reached {phase}: {state}")


class KeyTests(DeskCase):
    async def test_the_key_form_and_its_refusals(self):
        status, headers, body = await call(self.app, "GET", "/sign-in")
        self.assertEqual(status, 200)
        self.assertIn(b'name="key"', body)
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")
        status, _, body = await self.enter("w" * 40)
        self.assertEqual(status, 400)
        self.assertIn(b"not accepted", body)
        # Only this page may post a key: another site, a sandboxed frame
        # ("null") and a post without an origin are all refused.
        for origin in ("https://evil.example", "null"):
            status, _, _ = await self.enter(headers={"origin": origin})
            self.assertEqual(status, 403, origin)
        status, _, _ = await call(
            self.app,
            "POST",
            "/sign-in",
            headers={"content-type": "application/x-www-form-urlencoded"},
            body=urlencode({"key": KEY}).encode(),
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.desk.visit)

    async def test_wrong_keys_are_limited_per_address(self):
        for _ in range(10):
            await self.enter("w" * 40)
        status, _, _ = await self.enter()
        self.assertEqual(status, 429)
        status, _, _ = await self.enter(headers={"x-forwarded-for": "198.51.100.7"})
        self.assertEqual(status, 303)

    async def test_the_right_key_starts_a_private_visit(self):
        status, headers, _ = await self.enter()
        self.assertEqual((status, headers["location"]), (303, "/sign-in"))
        cookie = headers["set-cookie"]
        for part in ("HttpOnly", "Path=/sign-in", "SameSite=strict"):
            self.assertIn(part.lower(), cookie.lower())
        self.assertNotIn("secure", cookie.lower())
        _, headers, _ = await self.enter(headers={"x-forwarded-proto": "https"})
        self.assertIn("secure", headers["set-cookie"].lower())
        token = headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        status, _, body = await call(self.app, "GET", "/sign-in", cookie=token)
        self.assertIn(b'src="/sign-in/view.js"', body)
        self.assertIn(f"<strong>{HOST}</strong>".encode(), body)
        status, headers, body = await call(self.app, "GET", "/sign-in/view.js")
        self.assertEqual(headers["content-type"].split(";")[0], "text/javascript")

    async def test_a_newer_visit_replaces_the_older(self):
        first = await self.visit()
        second = await self.visit()
        status, _, _ = await call(self.app, "GET", "/sign-in/status", cookie=first)
        self.assertEqual(status, 401)
        status, _, body = await call(self.app, "GET", "/sign-in", cookie=first)
        self.assertIn(b'name="key"', body)
        self.assertEqual((await self.status(second))["phase"], "idle")

    async def test_visits_expire(self):
        cookie = await self.visit()
        self.desk.visit.expires = 0
        status, _, _ = await call(self.app, "GET", "/sign-in/status", cookie=cookie)
        self.assertEqual(status, 401)

    async def test_scripts_calls_need_this_page_and_json(self):
        cookie = await self.visit()
        for headers, body in (
            ({}, {}),
            ({"origin": "null"}, {}),
            ({"origin": "https://evil.example"}, {}),
            ({**ORIGIN, "content-type": "text/plain"}, b"{}"),
        ):
            with self.subTest(headers=headers):
                status, _, _ = await call(
                    self.app,
                    "POST",
                    "/sign-in/start",
                    headers=headers,
                    body=body,
                    cookie=cookie,
                )
                self.assertEqual(status, 403)
        status, _, _ = await call(
            self.app, "POST", "/sign-in/start", headers=ORIGIN, body={}
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.service.jobs, {})


class SignInTests(DeskCase):
    async def start(self, cookie):
        status, _, body = await call(
            self.app, "POST", "/sign-in/start", headers=ORIGIN, body={}, cookie=cookie
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)

    async def test_a_local_login_runs_and_the_page_says_when_it_is_done(self):
        cookie = await self.visit()
        self.assertEqual((await self.start(cookie))["message"], "Opening SMU sign-in…")
        state = await self.until(cookie, "starting")
        for _ in range(100):
            if state["message"] == "Checking your SMU sign-in…":
                break
            await asyncio.sleep(0.01)
            state = await self.status(cookie)
        self.assertEqual(state["message"], "Checking your SMU sign-in…")
        # A second start joins the running login rather than queueing another.
        await self.start(cookie)
        self.assertEqual(len(self.service.jobs), 1)
        status, _, _ = await call(self.app, "GET", "/sign-in/frame", cookie=cookie)
        self.assertEqual(status, 204)
        status, _, _ = await call(
            self.app,
            "POST",
            "/sign-in/input",
            headers=ORIGIN,
            body={"kind": "key", "key": "Enter"},
            cookie=cookie,
        )
        self.assertEqual(status, 409)
        self.backend.gate.set()
        state = await self.until(cookie, "signed_in")
        self.assertIsNone(state["next"])
        self.assertIn("close this page", state["message"])
        self.assertEqual(self.backend.calls[0][0], "login")
        # Local: a person is present, so the login may show Microsoft's pages.
        self.assertEqual(self.backend.origins, [False])

    async def test_signing_in_ends_the_remote_pause(self):
        self.service.sign_in_retry_at = self.service.clock() + 3600
        cookie = await self.visit()
        await self.start(cookie)
        self.backend.gate.set()
        await self.until(cookie, "signed_in")
        self.assertFalse(self.service.host_sign_in_required)

    async def test_a_failed_login_says_why_and_offers_another_try(self):
        self.backend.error = AuthenticationRequired(
            "x", reason="authentication_timeout"
        )
        self.backend.gate.set()
        cookie = await self.visit()
        await self.start(cookie)
        state = await self.until(cookie, "failed")
        self.assertTrue(state["retry"])
        self.assertIn("took too long", state["message"])
        self.backend.error = None
        await self.start(cookie)
        await self.until(cookie, "signed_in")
        self.assertEqual(len(self.service.jobs), 2)

    async def test_an_abandoned_sign_in_is_stopped(self):
        cookie = await self.visit()
        with (
            patch.object(signin_desk, "WATCH_INTERVAL", 0.02),
            patch.object(signin_desk, "IDLE_SECONDS", 0.05),
        ):
            await self.start(cookie)
            for _ in range(200):
                job = self.desk._job()
                if job.terminal:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual((await self.status(cookie))["phase"], "stopped")

    async def test_a_login_stopped_before_it_began_counts_as_stopped(self):
        cookie = await self.visit()
        await self.start(cookie)
        job = self.desk._job()
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
        self.assertEqual(job.status, "queued")  # it never ran, so says nothing
        self.assertEqual((await self.status(cookie))["phase"], "stopped")
        await self.start(cookie)
        self.assertEqual(len(self.service.jobs), 2)

    async def test_cancel_stops_the_login(self):
        cookie = await self.visit()
        await self.start(cookie)
        for _ in range(100):
            if self.desk._job().status == "running":
                break
            await asyncio.sleep(0.01)
        status, _, body = await call(
            self.app, "POST", "/sign-in/cancel", headers=ORIGIN, body={}, cookie=cookie
        )
        self.assertEqual((status, json.loads(body)), (200, {"next": None}))
        await asyncio.sleep(0.05)
        self.assertEqual(self.desk._job().status, "cancelled")
        status, _, _ = await call(self.app, "GET", "/sign-in/status", cookie=cookie)
        self.assertEqual(status, 401)


class LivePictureTests(DeskCase):
    """Tap, type and keys reach the page; nothing else does."""

    async def test_a_person_drives_the_sign_in_page_through_the_picture(self):
        from playwright.async_api import async_playwright

        cookie = await self.visit()
        await call(
            self.app, "POST", "/sign-in/start", headers=ORIGIN, body={}, cookie=cookie
        )
        async with async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Install Playwright Chromium to run browser tests")
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--host-resolver-rules=MAP * ~NOTFOUND", "--no-proxy-server"],
            )
            try:
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000}
                )
                page = await context.new_page()
                await page.set_content(STAND_IN)
                view = self.desk.view
                await view.attach(page, context)
                view.state = "email"
                self.assertEqual(page.viewport_size, live_view.VIEWPORT)

                async def send(body):
                    status, _, _ = await call(
                        self.app,
                        "POST",
                        "/sign-in/input",
                        headers=ORIGIN,
                        body=body,
                        cookie=cookie,
                    )
                    return status

                # Not Microsoft's page: no picture and no input.
                status, _, _ = await call(
                    self.app, "GET", "/sign-in/frame", cookie=cookie
                )
                self.assertEqual(status, 204)
                self.assertEqual(await send({"kind": "key", "key": "Tab"}), 409)

                with patch.object(
                    FBSClient, "_trusted_login_url", staticmethod(lambda url: True)
                ):
                    state = await self.status(cookie)
                    self.assertEqual(
                        (state["phase"], state["picture"], state["field"]),
                        ("signing_in", True, "email"),
                    )
                    status, headers, body = await call(
                        self.app, "GET", "/sign-in/frame", cookie=cookie
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["content-type"], "image/jpeg")
                    self.assertTrue(body.startswith(b"\xff\xd8"))
                    status, _, _ = await call(self.app, "GET", "/sign-in/frame")
                    self.assertEqual(status, 401)

                    box = await page.locator("#email").bounding_box()
                    size = page.viewport_size
                    tap = {
                        "kind": "tap",
                        "x": (box["x"] + box["width"] / 2) / size["width"],
                        "y": (box["y"] + box["height"] / 2) / size["height"],
                    }
                    self.assertEqual(await send(tap), 204)
                    self.assertEqual(
                        await send({"kind": "text", "text": "someone@smu.edu.sg"}), 204
                    )
                    self.assertEqual(
                        await page.input_value("#email"), "someone@smu.edu.sg"
                    )
                    self.assertEqual(await send({"kind": "key", "key": "Enter"}), 204)
                    self.assertEqual(await page.title(), "sent:someone@smu.edu.sg")

                    for refused in (
                        {"kind": "key", "key": "Control+L"},
                        {"kind": "key", "key": "F12"},
                        {"kind": "text", "text": "line\nbreak"},
                        {"kind": "text", "text": "x" * 257},
                        {"kind": "text", "text": ""},
                        {"kind": "tap", "x": 2, "y": 0.5},
                        {"kind": "tap", "x": "0.5", "y": 0.5},
                        {"kind": "script", "text": "alert(1)"},
                    ):
                        with self.subTest(refused=refused):
                            self.assertEqual(await send(refused), 400)

                    view.state = "password"
                    self.assertEqual((await self.status(cookie))["field"], "password")

                    # A link that opens a tab: the tab is closed at once.
                    await page.click("#away")
                    for _ in range(100):
                        if len(context.pages) == 1:
                            break
                        await asyncio.sleep(0.02)
                    self.assertEqual(len(context.pages), 1)

                await view.detach()
                self.assertEqual(page.viewport_size, {"width": 1440, "height": 1000})
                status, _, _ = await call(
                    self.app, "GET", "/sign-in/frame", cookie=cookie
                )
                self.assertEqual(status, 204)
            finally:
                await browser.close()


class PathTests(unittest.TestCase):
    def test_only_the_sign_in_paths_are_the_desks(self):
        for path in ("/sign-in", "/sign-in/frame", "/sign-in/view.js"):
            self.assertTrue(is_desk_path(path), path)
        for path in ("/sign-inx", "/sign", "/mcp", "/", "/oauth/sign-in"):
            self.assertFalse(is_desk_path(path), path)

    def test_sign_in_errors_carry_the_link(self):
        error = AuthenticationRequired("x", reason="sign_in_required_on_host")
        self.assertNotIn("sign_in_url", errors.public_failure(error))
        errors.set_sign_in_url(f"https://{HOST}/sign-in")
        self.addCleanup(errors.set_sign_in_url, None)
        for reason in ("sign_in_required_on_host", "session_renewal_pending"):
            failure = errors.public_failure(AuthenticationRequired("x", reason=reason))
            self.assertEqual(failure["sign_in_url"], f"https://{HOST}/sign-in")
            self.assertIn(f"https://{HOST}/sign-in", failure["next_action"])
        self.assertNotIn(
            "sign_in_url",
            errors.public_failure(
                AuthenticationRequired("x", reason="authentication_timeout")
            ),
        )


if __name__ == "__main__":
    unittest.main()
