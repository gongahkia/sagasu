"""Synthetic protocol fixtures; these are not recordings of live FBS traffic."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sagasu.fbs_client import AuthenticationRequired
from sagasu.fbs_http import (
    HTTPReadUnavailable,
    fbs_document_url,
    parse_account_document,
    read_account_http,
)
from sagasu.fbs_traffic import ReadTraffic
from sagasu.read_backends import HybridReads, equivalent_listing

URL = "https://fbs.intranet.smu.edu.sg/account-fixture.aspx"


def bookings_html(*, total=1, status="Confirmed", cancel=True):
    action = '<a id="row_BtnCancel">CANCEL</a>' if cancel else ""
    return f"""<html><body><div id="GridViewBooking">
    <table id="GridViewBooking_content"><tr class="row">
    <td>{action}</td><td>BK-FIXTURE</td><td>16-Nov-2026 10:00 - 11:00</td>
    <td>LKS</td><td>Room A</td><td></td><td></td><td></td><td>{status}</td>
    </tr></table><div>{total} ITEM(S) TOTAL</div></div></body></html>"""


def tasks_html(*, total=1):
    headers = [
        "REF. NUMBER",
        "DATE &amp; TIME OF USE",
        "FACILITY",
        "TYPE OF REQUEST",
        "REQUESTED BY",
        "STATUS",
        "",
    ]
    return (
        '<html><body><a href="#">Approve Selected</a><div id="GridViewTask"><table><tr>'
        + "".join(f"<th>{h}</th>" for h in headers)
        + f"""</tr></table>
    <table id="GridViewTask_content"><tr class="row"><td>BK-FIXTURE</td>
    <td>16-Nov-2026 10:00 - 11:00</td><td>Room A</td><td>Booking Approval</td>
    <td>A student</td><td>Pending Approval</td><td><input type="checkbox">
    <a id="row_BtnView">VIEW</a></td></tr></table>{total} ITEM(S) TOTAL</div></body></html>"""
    )


def observation(frame):
    traffic = Mock(document_url=Mock(return_value=URL))
    traffic.__enter__ = Mock(return_value=traffic)
    traffic.__exit__ = Mock(return_value=False)
    return traffic


class DocumentTests(unittest.TestCase):
    def test_bookings_preserve_status_and_permission(self):
        result = parse_account_document(
            "bookings", bookings_html(status="Pending Confirmation")
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["bookings"][0]["fbs_status"], "Pending Confirmation")
        self.assertTrue(result["bookings"][0]["can_cancel"])
        hidden = bookings_html().replace(
            'id="row_BtnCancel"', 'id="row_BtnCancel" style="display: none"'
        )
        self.assertFalse(
            parse_account_document("bookings", hidden)["bookings"][0]["can_cancel"]
        )

    def test_tasks_reuse_domain_columns_and_capabilities(self):
        result = parse_account_document("tasks", tasks_html())
        self.assertTrue(result["tasks"][0]["can_approve"])
        self.assertTrue(result["tasks"][0]["can_view"])
        disabled = tasks_html().replace('type="checkbox"', 'type="checkbox" disabled')
        self.assertFalse(
            parse_account_document("tasks", disabled)["tasks"][0]["can_approve"]
        )

    def test_paging_unloaded_duplicate_and_changed_rows_are_rejected(self):
        row = (
            '<tr class="row">'
            + bookings_html().split('<tr class="row">')[1].split("</table>")[0]
        )
        duplicate = bookings_html().replace("</table>", row + "</table>")
        cases = [
            bookings_html(total=3),
            bookings_html(total=0),
            bookings_html().replace("BK-FIXTURE", ""),
            bookings_html().replace("1 ITEM(S) TOTAL", ""),
            "<html>sign in</html>",
            duplicate,
            tasks_html().replace("REF. NUMBER", "OTHER"),
            tasks_html(total=2),
        ]
        for html in cases:
            kind = "tasks" if "GridViewTask" in html else "bookings"
            with self.subTest(html=html[:60]), self.assertRaises(HTTPReadUnavailable):
                parse_account_document(kind, html)

    def test_empty_listing_needs_explicit_total_and_empty_state(self):
        html = '<div id="GridViewBooking"><table id="GridViewBooking_content"><tr class="row"><td colspan="9">No items</td></tr></table>0 ITEM(S) TOTAL</div>'
        self.assertEqual(parse_account_document("bookings", html)["bookings"], [])
        with self.assertRaises(HTTPReadUnavailable):
            parse_account_document("bookings", html.replace("No items", ""))

    def test_parity_includes_status_permissions_reference_and_coverage(self):
        result = parse_account_document("bookings", bookings_html())
        self.assertTrue(equivalent_listing("bookings", result, result))
        for other in (
            parse_account_document("bookings", bookings_html(status="Cancelled")),
            parse_account_document("bookings", bookings_html(cancel=False)),
            {**result, "complete": False},
            {
                **result,
                "bookings": [{**result["bookings"][0], "reference_number": "OTHER"}],
            },
        ):
            self.assertFalse(equivalent_listing("bookings", result, other))

    def test_endpoint_origin_checks(self):
        self.assertTrue(fbs_document_url(URL))
        for url in [
            URL.replace("https:", "http:"),
            URL.replace("fbs.", "evil."),
            URL + "#token",
            URL.replace("https://", "https://user@"),
            URL.replace(".sg/", ".sg:444/"),
            URL + "\\secret",
            "invalid",
        ]:
            self.assertFalse(fbs_document_url(url))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_get_uses_session_context_and_never_follows_redirects(self):
        response = Mock(
            status=200,
            url=URL,
            headers={"content-type": "text/html; charset=utf-8"},
            body=AsyncMock(return_value=bookings_html().encode()),
            dispose=AsyncMock(),
        )
        client = SimpleNamespace(
            context=SimpleNamespace(
                request=SimpleNamespace(get=AsyncMock(return_value=response))
            )
        )
        result = await read_account_http(client, "bookings", URL)
        self.assertEqual(result["total"], 1)
        client.context.request.get.assert_awaited_once_with(
            URL,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            timeout=10000,
            max_redirects=0,
            max_retries=0,
            fail_on_status_code=False,
        )
        response.dispose.assert_awaited_once()

    async def test_redirect_auth_and_oversize_fail_without_parsing_and_dispose(self):
        for status, headers, error in [
            (
                302,
                {"location": "https://evil.test/PRIVATE_TOKEN"},
                AuthenticationRequired,
            ),
            (200, {"content-type": "application/json"}, HTTPReadUnavailable),
            (
                200,
                {"content-type": "text/html", "content-length": "999999999"},
                HTTPReadUnavailable,
            ),
        ]:
            response = Mock(
                status=status,
                url=URL,
                headers=headers,
                body=AsyncMock(),
                dispose=AsyncMock(),
            )
            client = SimpleNamespace(
                context=SimpleNamespace(
                    request=SimpleNamespace(get=AsyncMock(return_value=response))
                )
            )
            with self.assertRaises(error):
                await read_account_http(client, "bookings", URL)
            response.body.assert_not_awaited()
            response.dispose.assert_awaited_once()

    async def test_untrusted_url_is_rejected_before_request(self):
        get = AsyncMock()
        client = SimpleNamespace(
            context=SimpleNamespace(request=SimpleNamespace(get=get))
        )
        with self.assertRaises(HTTPReadUnavailable):
            await read_account_http(client, "bookings", "https://elsewhere.test/")
        get.assert_not_awaited()


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.result = parse_account_document("bookings", bookings_html())
        self.client = SimpleNamespace(
            context=object(), page=Mock(), search=AsyncMock(), book=AsyncMock()
        )
        self.browser = AsyncMock(return_value=self.result)
        self.http = AsyncMock(return_value=self.result)
        self.router = HybridReads("auto")
        self.patches = [
            patch("sagasu.read_backends.read_bookings", self.browser),
            patch("sagasu.read_backends.read_account_http", self.http),
            patch("sagasu.read_backends.ReadTraffic", return_value=observation(None)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_first_read_verifies_then_next_read_uses_http(self):
        first = await self.router.account(self.client, "bookings")
        self.assertEqual(first["read_backend"]["name"], "browser")
        self.assertEqual(first["read_backend"]["api_verification"], "verified")
        second = await self.router.account(self.client, "bookings")
        self.assertEqual(second["read_backend"]["name"], "api")
        self.browser.assert_awaited_once()
        self.assertEqual(self.http.await_count, 2)

    async def test_mismatched_data_never_enables_http(self):
        self.http.return_value = parse_account_document(
            "bookings", bookings_html(status="Cancelled")
        )
        result = await self.router.account(self.client, "bookings")
        self.assertEqual(
            result["read_backend"]["fallback_reason"], "browser_http_mismatch"
        )
        self.assertEqual(self.router.capabilities()["api"]["verified"], [])

    async def test_http_failure_falls_back_once_and_invalidates(self):
        await self.router.account(self.client, "bookings")
        self.http.side_effect = HTTPReadUnavailable("incomplete_listing")
        result = await self.router.account(self.client, "bookings")
        self.assertEqual(result["read_backend"]["api_verification"], "invalidated")
        self.assertEqual(self.browser.await_count, 2)
        self.assertEqual(self.http.await_count, 2)

    async def test_browser_errors_and_cancellation_are_not_swallowed(self):
        self.browser.side_effect = RuntimeError("implementation bug")
        with self.assertRaises(RuntimeError):
            await self.router.account(self.client, "bookings")
        self.http.assert_not_awaited()
        self.browser.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.router.account(self.client, "bookings")

    async def test_strict_api_has_no_implicit_browser_or_search(self):
        strict = HybridReads("api")
        with self.assertRaises(HTTPReadUnavailable):
            await strict.account(self.client, "bookings")
        with self.assertRaises(HTTPReadUnavailable):
            await strict.search(self.client, {})
        self.browser.assert_not_awaited()
        self.client.search.assert_not_awaited()

    async def test_browser_mode_does_not_probe_http(self):
        result = await HybridReads("browser").account(self.client, "bookings")
        self.assertEqual(result["read_backend"]["name"], "browser")
        self.http.assert_not_awaited()

    async def test_context_replacement_and_verification_expiry_need_new_parity(self):
        await self.router.account(self.client, "bookings")
        self.client.context = object()
        await self.router.account(self.client, "bookings")
        self.assertEqual(self.browser.await_count, 2)
        self.router.verification_ttl = 0
        await self.router.account(self.client, "bookings")
        self.assertEqual(self.browser.await_count, 3)

    async def test_write_kinds_never_dispatch_or_fallback(self):
        for kind in ["book", "cancel", "approve_task", "validate", "reconcile"]:
            with self.assertRaises(HTTPReadUnavailable):
                await self.router.account(self.client, kind)
        self.http.assert_not_awaited()
        self.browser.assert_not_awaited()
        self.client.book.assert_not_awaited()


class TrafficTests(unittest.TestCase):
    def test_assets_and_report_limit_do_not_hide_document_candidates(self):
        page = Mock()
        frame = SimpleNamespace(name="frameContent", url=URL)
        with ReadTraffic(page, limit=1) as traffic:
            for resource in ["image", "stylesheet", "script"]:
                traffic._response(
                    SimpleNamespace(
                        request=SimpleNamespace(url=URL, resource_type=resource)
                    )
                )
            xhr = SimpleNamespace(
                url=URL,
                method="POST",
                resource_type="xhr",
                post_data="__VIEWSTATE=PRIVATE",
            )
            response = SimpleNamespace(
                request=xhr, status=200, headers={"content-type": "text/plain"}
            )
            traffic._response(response)
            request = SimpleNamespace(
                url=URL, method="GET", resource_type="document", frame=frame
            )
            traffic._response(
                SimpleNamespace(
                    request=request, status=200, headers={"content-type": "text/html"}
                )
            )
            self.assertEqual(traffic.document_url(frame), URL)
            report = traffic.report()
        self.assertTrue(report["truncated"])
        self.assertEqual(report["responses_observed"], 2)
        self.assertEqual(report["ignored_asset_responses"], 3)
        self.assertEqual(report["aspnet_postbacks"], 1)

    def test_passive_report_never_contains_urls_bodies_or_secret_parameters(self):
        page = Mock()
        frame = SimpleNamespace(name="frameContent", url=URL + "?token=SECRET_QUERY")
        request = SimpleNamespace(
            url=frame.url,
            method="GET",
            resource_type="document",
            frame=frame,
            post_data=None,
        )
        response = SimpleNamespace(
            request=request,
            status=200,
            headers={"content-type": "text/html", "set-cookie": "SECRET_COOKIE"},
        )
        with ReadTraffic(page, limit=2) as traffic:
            traffic._response(response)
            self.assertEqual(traffic.document_url(frame), frame.url)
            request = SimpleNamespace(
                url=URL + "/SECRET_PATH",
                method="POST",
                resource_type="xhr",
                post_data="__VIEWSTATE=SECRET_BODY&password=SECRET_PASSWORD",
            )
            traffic._response(
                SimpleNamespace(
                    request=request,
                    status=200,
                    headers={"content-type": "application/json"},
                )
            )
            traffic._response(response)
            report = traffic.report()
        encoded = json.dumps(report)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("account-fixture", encoded)
        self.assertEqual(report["json_responses"], 1)
        self.assertEqual(report["aspnet_postbacks"], 1)
        self.assertTrue(report["truncated"])
        self.assertEqual(traffic._documents, [])
        page.remove_listener.assert_called_once()
