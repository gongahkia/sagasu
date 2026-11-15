import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import Client

from sagasu.fbs_client import AuthenticationRequired
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
