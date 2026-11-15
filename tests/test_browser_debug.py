"""Local browser/protocol checks; no live FBS requests or actions."""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from mcp import Client
from playwright.async_api import async_playwright

from sagasu.action_journal import ActionJournal
from sagasu.browser_debug import ScreenshotUnavailable, capture_browser_screenshot
from sagasu.fbs_client import FBS_URL
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService, Backend


class BrowserDebugTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_capture_precedes_cleanup_and_keeps_first_failure_image(
        self,
    ):
        from sagasu.observability import observed_step
        from sagasu.operation_diagnostics import diagnostic_scope, capture_failure

        @observed_step("fixture.failure")
        async def fail(client):
            raise ValueError("PRIVATE FAILURE")

        with diagnostic_scope() as capture:
            try:
                await fail(self.client)
            except ValueError:
                await self.page.goto("about:blank")
            first = capture.screenshot
            await capture_failure(self.client, ValueError("SECOND"))
        self.assertIs(first, capture.screenshot)
        self.assertTrue(first["receipt_png"].startswith(b"\x89PNG"))
        self.assertEqual(
            first["scope"], "first_observed_browser_failure_before_cleanup"
        )
        self.assertEqual(self.page.url, "about:blank")

    async def asyncSetUp(self):
        driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        browser = await driver.chromium.launch(headless=True)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page(viewport={"width": 320, "height": 200})
        self.html = '<body style="margin:0;background:white"><input id="secret" value="PRIVATE VALUE" style="position:absolute;left:20px;top:20px;width:120px;height:40px"></body>'
        self.requests = []

        async def route(request):
            self.requests.append(request.request.url)
            if "appexception.aspx" in request.request.url:
                await request.abort("namenotresolved")
            elif "foreign.test" in request.request.url:
                await request.fulfill(
                    body='<body style="margin:0;background:red">PRIVATE FOREIGN CONTENT</body>',
                    content_type="text/html",
                )
            else:
                await request.fulfill(body=self.html, content_type="text/html")

        await self.page.route("**/*", route)
        await self.page.goto(FBS_URL)
        self.client = SimpleNamespace(page=self.page)

    async def pixel(self, png, x, y):
        return await self.page.evaluate(
            """async ({png,x,y}) => {
            const img = new Image(); img.src = 'data:image/png;base64,' + png;
            await img.decode(); const canvas = document.createElement('canvas');
            canvas.width=img.width; canvas.height=img.height;
            const ctx=canvas.getContext('2d'); ctx.drawImage(img,0,0);
            return [...ctx.getImageData(x,y,1,1).data];
        }""",
            {"png": base64.b64encode(png).decode(), "x": x, "y": y},
        )

    async def test_form_values_are_masked_in_png_without_changing_page_or_navigating(
        self,
    ):
        requests = len(self.requests)
        result = await capture_browser_screenshot(self.client)
        self.assertEqual(
            await self.pixel(result["receipt_png"], 50, 40), [0, 0, 0, 255]
        )
        self.assertEqual(
            await self.pixel(result["receipt_png"], 10, 180), [255, 255, 255, 255]
        )
        self.assertEqual(
            await self.page.locator("#secret").input_value(), "PRIVATE VALUE"
        )
        self.assertEqual(len(self.requests), requests)
        self.assertEqual(result["receipt_kind"], "browser_debug")
        self.assertNotIn(
            "PRIVATE",
            json.dumps({k: v for k, v in result.items() if k != "receipt_png"}),
        )

    async def test_foreign_frame_is_masked_as_a_whole(self):
        self.html += '<iframe src="https://foreign.test/" style="position:absolute;left:180px;top:20px;width:100px;height:60px;border:0"></iframe>'
        await self.page.goto(FBS_URL)
        result = await capture_browser_screenshot(self.client)
        self.assertEqual(result["external_frames_masked"], 1)
        self.assertEqual(
            await self.pixel(result["receipt_png"], 210, 40), [0, 0, 0, 255]
        )

    async def test_unattributed_aborted_frame_is_masked(self):
        self.html += (
            '<iframe name="frameContent" src="https://appexception.aspx/"></iframe>'
        )
        await self.page.goto(FBS_URL)
        result = await capture_browser_screenshot(self.client)
        # intercepted aborts lose their destination URL; hide the unknown frame.
        self.assertEqual(result["external_frames_masked"], 1)
        self.assertTrue(result["receipt_png"].startswith(b"\x89PNG"))

    async def test_non_fbs_top_pages_are_refused_before_capture(self):
        for url in [
            "about:blank",
            "https://login.microsoftonline.com/common/",
            "https://foreign.test/",
        ]:
            await self.page.goto(url)
            with patch.object(self.page, "screenshot", AsyncMock()) as screenshot:
                with self.assertRaises(ScreenshotUnavailable):
                    await capture_browser_screenshot(self.client)
                screenshot.assert_not_awaited()

    async def test_authentication_controls_are_refused_before_capture(self):
        for control in [
            '<input type="password" value="PRIVATE">',
            '<input autocomplete="one-time-code" value="123456">',
            '<span id="idRichContext_DisplaySign">88</span>',
        ]:
            self.html = control
            await self.page.goto(FBS_URL)
            with patch.object(self.page, "screenshot", AsyncMock()) as screenshot:
                with self.assertRaises(ScreenshotUnavailable):
                    await capture_browser_screenshot(self.client)
                screenshot.assert_not_awaited()

    async def test_authentication_iframe_is_refused_before_capture(self):
        self.html += '<iframe src="https://login.microsoftonline.com/common/"></iframe>'
        # the child uses a separate fixture to avoid recursively nesting itself.
        await self.page.route(
            "https://login.microsoftonline.com/**",
            lambda route: route.fulfill(
                body="<span>MFA PRIVATE</span>", content_type="text/html"
            ),
        )
        await self.page.goto(FBS_URL)
        with patch.object(self.page, "screenshot", AsyncMock()) as screenshot:
            with self.assertRaises(ScreenshotUnavailable):
                await capture_browser_screenshot(self.client)
            screenshot.assert_not_awaited()

    async def test_navigation_during_capture_discards_the_png(self):
        original = self.page.screenshot

        async def changed(**kwargs):
            png = await original(**kwargs)
            await self.page.goto("https://login.microsoftonline.com/common/")
            return png

        with patch.object(self.page, "screenshot", changed):
            with self.assertRaises(ScreenshotUnavailable):
                await capture_browser_screenshot(self.client)

    async def test_mcp_returns_a_separate_image_without_persisting_it_in_action_history(
        self,
    ):
        owner = self

        class DebugBackend:
            async def run(self, kind, payload, progress, notify):
                owner.assertEqual(kind, "screenshot")
                return await capture_browser_screenshot(owner.client)

        with tempfile.TemporaryDirectory() as temporary:
            journal = ActionJournal(Path(temporary) / "history.sqlite3")
            service = AgentService(backend=DebugBackend(), journal=journal)
            self.addAsyncCleanup(service.close)
            async with Client(create_server(service)) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                self.assertTrue(tools["browser_screenshot"].annotations.read_only_hint)
                self.assertEqual(
                    tools["browser_screenshot"].input_schema.get("properties", {}), {}
                )
                job = (
                    await client.call_tool("browser_screenshot", {})
                ).structured_content
                await service.get(job["operation_id"]).task
                result = service.result(job["operation_id"])
                self.assertTrue(result["receipt_available"])
                self.assertNotIn("receipt_png", json.dumps(result))
                receipt = await client.call_tool(
                    "operation_receipt", {"operation_id": job["operation_id"]}
                )
                images = [c for c in receipt.content if c.type == "image"]
                self.assertEqual(len(images), 1)
                self.assertTrue(base64.b64decode(images[0].data).startswith(b"\x89PNG"))
                self.assertEqual(journal.history()["total"], 0)

    async def test_capture_waits_for_the_existing_browser_queue(self):
        entered, release, captured = asyncio.Event(), asyncio.Event(), asyncio.Event()
        owner = self

        class QueueBackend:
            async def run(self, kind, payload, progress, notify):
                if kind == "search":
                    entered.set()
                    await release.wait()
                    return {}
                captured.set()
                return await capture_browser_screenshot(owner.client)

        service = AgentService(backend=QueueBackend())
        self.addAsyncCleanup(service.close)
        first = service.start("search")
        await entered.wait()
        second = service.start("screenshot")
        await asyncio.sleep(0)
        self.assertFalse(captured.is_set())
        release.set()
        await asyncio.gather(
            service.get(first["operation_id"]).task,
            service.get(second["operation_id"]).task,
        )
        self.assertEqual(service.get(second["operation_id"]).status, "succeeded")


class DebugBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_native_error_returns_only_bounded_browser_error_codes(self):
        controls = Mock(count=AsyncMock(return_value=0))
        trusted = Mock(url=FBS_URL)
        trusted.locator.return_value = controls
        native = Mock(
            url="https://appexception.aspx/",
            evaluate=AsyncMock(return_value="chrome-error:"),
        )
        native.locator.return_value.all_text_contents = AsyncMock(
            return_value=["ERR_NAME_NOT_RESOLVED", "PRIVATE ERROR TEXT"]
        )
        page = Mock(
            url=FBS_URL,
            is_closed=Mock(return_value=False),
            frames=[trusted, native],
            main_frame=trusted,
            screenshot=AsyncMock(return_value=b"png"),
        )
        result = await capture_browser_screenshot(SimpleNamespace(page=page))
        self.assertEqual(result["browser_error_codes"], ["ERR_NAME_NOT_RESOLVED"])
        self.assertNotIn(
            "PRIVATE",
            json.dumps({k: v for k, v in result.items() if k != "receipt_png"}),
        )

    async def test_capture_does_not_authenticate_even_with_normal_backend_autologin_enabled(
        self,
    ):
        context = AsyncMock()
        with (
            patch("sagasu.mcp_service.FBSClient", return_value=context) as browser,
            patch(
                "sagasu.browser_debug.capture_browser_screenshot",
                AsyncMock(return_value={}),
            ),
        ):
            await Backend().run("screenshot", None, AsyncMock(), AsyncMock())
        self.assertFalse(browser.call_args.kwargs["interactive"])
        context.__aenter__.return_value.ensure_authenticated.assert_not_awaited()
