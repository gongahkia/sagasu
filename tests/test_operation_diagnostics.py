import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import Client

from sagasu.fbs_client import AuthenticationRequired
from sagasu.browser_debug import ScreenshotUnavailable
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentService, Backend
from sagasu.observability import event, observed_step
from sagasu.operation_diagnostics import (
    capture_failure,
    diagnostic_scope,
    support_bundle,
)


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_omits_all_values_except_allowlisted_timings_and_code_locations(
        self,
    ):
        with diagnostic_scope() as scope:
            for _ in range(110):
                event(
                    "private.test",
                    purpose="SECRET PURPOSE",
                    email="private@smu.edu.sg",
                    url="https://example.test/?token=SECRET",
                    error_type="LayoutChanged",
                    duration_ms=12,
                )
        job = SimpleNamespace(
            id="op",
            kind="search",
            status="failed",
            terminal=True,
            created_at=0,
            updated_at=1,
            error={"code": "LayoutChanged", "message": "SECRET"},
            recovered=False,
            diagnostic_events=scope.events,
            diagnostic_dropped=scope.dropped,
            failure_screenshot=None,
        )
        bundle = support_bundle(
            job,
            {
                "operational": True,
                "checks": [
                    {
                        "name": "config.file",
                        "status": "pass",
                        "message": "SECRET",
                        "hint": "SECRET",
                    }
                ],
                "values": "SECRET",
            },
        )
        self.assertEqual(len(bundle["events"]), 100)
        self.assertEqual(bundle["dropped_events"], 10)
        self.assertEqual(bundle["events"][0]["duration_ms"], 12)
        self.assertNotIn("SECRET", json.dumps(bundle))
        self.assertNotIn("private@", json.dumps(bundle))

    async def test_authentication_and_missing_browser_are_not_captured(self):
        with (
            diagnostic_scope() as scope,
            patch(
                "sagasu.browser_debug.capture_browser_screenshot", AsyncMock()
            ) as screenshot,
        ):
            await capture_failure(
                SimpleNamespace(page=object()), AuthenticationRequired("SECRET")
            )
            await capture_failure(
                SimpleNamespace(page=object()), ScreenshotUnavailable("SECRET")
            )
            await capture_failure(SimpleNamespace(page=None), ValueError("SECRET"))
            screenshot.assert_not_awaited()
            self.assertIsNone(scope.screenshot)

    async def test_failed_capture_never_changes_original_exception_or_recurses(self):
        @observed_step("test.failure")
        async def fail(client):
            raise ValueError("ORIGINAL")

        with (
            diagnostic_scope() as scope,
            patch(
                "sagasu.browser_debug.capture_browser_screenshot",
                AsyncMock(side_effect=RuntimeError("CAPTURE")),
            ) as screenshot,
        ):
            with self.assertRaisesRegex(ValueError, "ORIGINAL"):
                await fail(SimpleNamespace(page=object()))
            await capture_failure(SimpleNamespace(page=object()), ValueError())
            screenshot.assert_awaited_once()
            self.assertIsNone(scope.screenshot)

    async def test_concurrent_operations_retain_only_their_own_bounded_traces(self):
        async def worker(name):
            with diagnostic_scope() as scope:
                event(name)
                await asyncio.sleep(0)
                event(name)
                return scope.events

        a, b = await asyncio.gather(worker("test.a"), worker("test.b"))
        self.assertEqual({e["event"] for e in a}, {"test.a"})
        self.assertEqual({e["event"] for e in b}, {"test.b"})

    async def test_failure_image_attaches_to_exact_job_and_bundle_is_local_and_image_free(
        self,
    ):
        @observed_step("test.browser_failure")
        async def fail(client):
            raise ValueError("PRIVATE EXCEPTION")

        class FailingBackend:
            async def run(self, *args):
                await fail(SimpleNamespace(page=object()))

        service = AgentService(backend=FailingBackend())
        self.addAsyncCleanup(service.close)
        image = {
            "receipt_png": b"\x89PNG\r\n\x1a\nPRIVATE IMAGE",
            "captured_at": "2099-11-01T10:00:00+08:00",
        }
        with patch(
            "sagasu.browser_debug.capture_browser_screenshot",
            AsyncMock(return_value=image),
        ):
            operation = service.start("bookings")
            job = service.get(operation["operation_id"])
            await job.task
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.public()["next_tool"], "support_bundle")
        self.assertTrue(job.public()["failure_screenshot_available"])
        setup = {"operational": True, "checks": []}
        async with Client(
            create_server(service, configuration_provider=lambda: setup)
        ) as client:
            result = await client.call_tool("support_bundle", {"operation_id": job.id})
            self.assertFalse(result.is_error)
            self.assertNotIn("PRIVATE", json.dumps(result.structured_content))
            result = await client.call_tool(
                "operation_receipt", {"operation_id": job.id, "failure": True}
            )
            self.assertFalse(result.is_error)
            self.assertTrue(any(c.type == "image" for c in result.content))
        self.assertIsNone(job.receipt)

    async def test_noninteractive_booking_validation_propagates_to_native_client(self):
        with patch(
            "sagasu.mcp_service.execute_booking", AsyncMock(return_value={})
        ) as execute:
            await Backend(interactive_auth=False).run(
                "validate", object(), AsyncMock(), AsyncMock()
            )
        self.assertFalse(execute.call_args.kwargs["interactive"])


class WatchDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def run_watch(self, *, recover=False, capture_error=False, checks=2):
        from sagasu.fbs_client import LayoutChanged
        from tests.test_booking_watch import REF, listing

        @observed_step("fixture.watch_failure")
        async def fail(client):
            raise LayoutChanged("SECRET PAGE TEXT")

        class WatchBackend:
            count = 0

            async def run(inner, kind, payload, progress, notify):
                inner.count += 1
                if recover and inner.count > 1:
                    return listing("Pending Approval")
                await fail(SimpleNamespace(page=object()))

        service = AgentService(backend=WatchBackend())
        self.addAsyncCleanup(service.close)
        now = [0]

        async def sleep(seconds):
            now[0] += seconds
            await asyncio.sleep(0)

        image = {
            "receipt_png": b"\x89PNG\r\n\x1a\nPRIVATE IMAGE",
            "captured_at": "2099-11-01T10:00:00+08:00",
        }
        with (
            patch("sagasu.booking_watch.monotonic", side_effect=lambda: now[0]),
            patch("sagasu.booking_watch.sleep", side_effect=sleep),
            patch(
                "sagasu.browser_debug.capture_browser_screenshot",
                (
                    AsyncMock(side_effect=RuntimeError("SECRET CAPTURE"))
                    if capture_error
                    else AsyncMock(return_value=image)
                ),
            ),
        ):
            started = service.watch_booking(
                REF, interval_seconds=15, timeout_seconds=checks * 15
            )
            job = service.get(started["operation_id"])
            await job.task
        return service, job

    async def test_watch_retry_retains_failure_trace_and_image_after_success(self):
        service, job = await self.run_watch(recover=True)
        self.assertTrue(job.result["target_status_observed"])
        bundle = support_bundle(job, {"checks": []})
        self.assertEqual(bundle["latest_failed_check"]["check"], 1)
        self.assertEqual(bundle["latest_failed_check"]["error_type"], "LayoutChanged")
        self.assertEqual(bundle["failure_image"]["check"], 1)
        self.assertTrue(bundle["failure_image"]["form_fields_masked"])
        self.assertEqual(bundle["watch_checks"][-1]["error_type"], None)
        self.assertIn("watch.queue", str(bundle["events"]))
        self.assertIn("fixture.watch_failure", str(bundle["latest_failed_check"]))
        self.assertNotIn("SECRET", json.dumps(bundle))
        self.assertNotIn("PRIVATE", json.dumps(bundle))
        self.assertNotIn("BK-", json.dumps(bundle))
        async with Client(create_server(service)) as client:
            response = await client.call_tool(
                "support_bundle", {"operation_id": job.id}
            )
            self.assertFalse(response.is_error)
            response = await client.call_tool(
                "operation_receipt", {"operation_id": job.id, "failure": True}
            )
            self.assertFalse(response.is_error)
            self.assertTrue(any(c.type == "image" for c in response.content))

    async def test_watch_capture_failure_is_reported_without_changing_read_outcome(
        self,
    ):
        _, job = await self.run_watch(capture_error=True)
        self.assertEqual(job.result["outcome"], "timed_out")
        bundle = support_bundle(job, {})
        self.assertFalse(bundle["failure_screenshot_available"])
        self.assertEqual(bundle["screenshot_status"], "capture_failed")
        self.assertEqual(bundle["latest_failed_check"]["error_type"], "LayoutChanged")
        self.assertNotIn("SECRET", json.dumps(bundle))

    async def test_watch_history_is_bounded_and_image_check_matches_first_capture(self):
        _, job = await self.run_watch(checks=40)
        bundle = support_bundle(job, {})
        self.assertEqual(len(bundle["watch_checks"]), 20)
        self.assertLessEqual(len(bundle["events"]), 100)
        self.assertGreater(bundle["dropped_events"], 0)
        self.assertEqual(bundle["failure_image"]["check"], 1)
        self.assertEqual(bundle["latest_failed_check"]["check"], 40)
        self.assertNotIn("SECRET", json.dumps(bundle))

    async def test_backend_selection_and_exception_chain_are_allowlisted(self):
        from sagasu.read_backends import HybridReads

        with diagnostic_scope() as scope:
            HybridReads._result({}, "api")
            HybridReads._result({}, "browser")
            event("test.malformed_metadata", backend={"SECRET": "value"})
            event(
                "test.error",
                backend="https://SECRET",
                error_chain=["LayoutChanged", "https://SECRET"],
                error_type="LayoutChanged",
            )
        self.assertEqual(
            [e.get("backend") for e in scope.events[:2]], ["api", "browser"]
        )
        self.assertNotIn("SECRET", json.dumps(scope.events))
        self.assertEqual(scope.events[-1]["error_chain"], ["LayoutChanged"])

    async def test_unavailable_capture_explains_authentication_and_missing_page(self):
        with diagnostic_scope() as scope:
            await capture_failure(SimpleNamespace(page=None), ValueError("SECRET"))
            self.assertEqual(scope.screenshot_status, "no_browser_page")
            await capture_failure(
                SimpleNamespace(page=object()), AuthenticationRequired("SECRET")
            )
            self.assertEqual(
                scope.screenshot_status, "skipped_authentication_or_cancellation"
            )

    async def test_normal_operation_retains_caught_failure_evidence_after_recovery(
        self,
    ):
        @observed_step("fixture.recovered_failure")
        async def fail(client):
            raise ValueError("SECRET")

        class RecoveringBackend:
            async def run(self, *args):
                try:
                    await fail(SimpleNamespace(page=object()))
                except ValueError:
                    return {"bookings": [], "complete": True}

        service = AgentService(backend=RecoveringBackend())
        self.addAsyncCleanup(service.close)
        with patch(
            "sagasu.browser_debug.capture_browser_screenshot",
            AsyncMock(
                return_value={
                    "receipt_png": b"PNG",
                    "captured_at": "2099-11-01T10:00:00+08:00",
                }
            ),
        ):
            started = service.start("bookings")
            job = service.get(started["operation_id"])
            await job.task
        self.assertEqual(job.status, "succeeded")
        self.assertTrue(job.public()["failure_screenshot_available"])
        bundle = support_bundle(job, {})
        self.assertEqual(bundle["screenshot_status"], "captured")
        self.assertNotIn("SECRET", json.dumps(bundle))

    async def test_invalid_or_oversized_image_has_a_bounded_reason(self):
        for png, status in [
            (None, "image_unavailable"),
            (b"x" * (4 * 1024 * 1024 + 1), "image_too_large"),
        ]:
            with (
                diagnostic_scope() as diagnostic,
                patch(
                    "sagasu.browser_debug.capture_browser_screenshot",
                    AsyncMock(return_value={"receipt_png": png}),
                ),
            ):
                await capture_failure(SimpleNamespace(page=object()), ValueError())
            self.assertIsNone(diagnostic.screenshot)
            self.assertEqual(diagnostic.screenshot_status, status)

    async def test_pagination_failure_exposes_counts_but_no_reference_or_grid(self):
        from sagasu.fbs_paging import collect_pages
        from sagasu.fbs_client import LayoutChanged

        page = {"bookings": [{"reference_number": "PRIVATE_REFERENCE"}], "total": 2}
        with (
            diagnostic_scope() as diagnostic,
            patch("sagasu.fbs_paging.move_page", AsyncMock(return_value=True)),
        ):
            with self.assertRaises(LayoutChanged):
                await collect_pages(
                    None, None, "PRIVATE_GRID", "bookings", AsyncMock(return_value=page)
                )
        self.assertEqual(
            diagnostic.events[-1]["event"], "paging.repeated_page_or_reference"
        )
        self.assertEqual(diagnostic.events[-1]["page"], 2)
        read = [e for e in diagnostic.events if e["event"] == "paging.page_read"]
        self.assertEqual(read[-1]["records_read"], 1)
        self.assertEqual(read[-1]["reported_total"], 2)
        self.assertNotIn("PRIVATE", json.dumps(diagnostic.events))

    async def test_pagination_fields_are_bounded_and_ignore_arbitrary_values(self):
        with diagnostic_scope() as diagnostic:
            event(
                "test.metadata",
                page=-1,
                row_count=True,
                records_read="SECRET",
                reported_total=1000001,
                direction={"SECRET": True},
            )
        self.assertEqual(diagnostic.events, [{"event": "test.metadata", "step": None}])
