"""Watch exact FBS references without writes, false readiness or queue starvation."""

import asyncio
import unittest
from unittest.mock import patch

from mcp import Client

from sagasu.mcp_service import AgentError, AgentService
from sagasu.fbs_client import AuthenticationRequired
from sagasu.mcp_server import create_server


REF = "BK-20991101-000143"


def listing(status="Pending Confirmation", **extra):
    return {
        "complete": True,
        "bookings": [{"reference_number": REF, "fbs_status": status}],
        **extra,
    }


class Backend:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def run(self, kind, payload, progress, notify):
        self.calls.append(kind)
        response = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response


class BookingWatchTests(unittest.IsolatedAsyncioTestCase):
    async def run_watch(self, responses, timeout=120):
        backend = Backend(*responses)
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        now = [0]

        async def sleep(seconds):
            self.assertFalse(service.queue.locked())
            now[0] += seconds
            await asyncio.sleep(0)

        with (
            patch("sagasu.booking_watch.monotonic", side_effect=lambda: now[0]),
            patch("sagasu.booking_watch.sleep", side_effect=sleep),
        ):
            started = service.watch_booking(REF, timeout_seconds=timeout)
            job = service.get(started["operation_id"])
            await job.task
        return service, job, backend

    async def test_waits_for_exact_pending_approval_once(self):
        service, job, backend = await self.run_watch(
            [
                listing(),
                listing("Pending Approval Required"),
                listing("  pending   APPROVAL  "),
                listing("Confirmed"),
            ]
        )
        self.assertEqual(job.status, "succeeded")
        self.assertTrue(job.result["target_status_observed"])
        self.assertEqual(job.result["outcome"], "pending_approval_observed")
        self.assertEqual(backend.calls, ["bookings"] * 3)
        await service.wait(job.id, 0)
        self.assertEqual(len(backend.calls), 3)

    async def test_unknown_or_incomplete_evidence_does_not_establish_readiness(self):
        cases = [
            listing("Pending Approval", complete=False),
            {"complete": True, "bookings": []},
            {
                "complete": True,
                "bookings": [
                    {"reference_number": "OTHER", "fbs_status": "Pending Approval"}
                ],
            },
            {"complete": True, "bookings": listing("Pending Approval")["bookings"] * 2},
            listing(""),
            RuntimeError("private FBS page text"),
        ]
        for response in cases:
            with self.subTest(response=type(response).__name__):
                _, job, _ = await self.run_watch([response], timeout=15)
                self.assertFalse(job.result["target_status_observed"])
                self.assertEqual(job.result["outcome"], "timed_out")
                self.assertNotIn("private FBS page text", str(job.result))

    async def test_transient_read_failure_recovers(self):
        _, job, backend = await self.run_watch(
            [OSError("private"), listing("Pending Approval")]
        )
        self.assertTrue(job.result["target_status_observed"])
        self.assertIsNone(job.result["last_read_error"])
        self.assertEqual(backend.calls, ["bookings", "bookings"])

    async def test_authentication_failure_stops_without_repeated_login_attempts(self):
        _, job, backend = await self.run_watch([AuthenticationRequired("private")])
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.result["outcome"], "blocked")
        self.assertFalse(job.result["target_status_observed"])
        self.assertEqual(job.error["next_tool"], "session_status")
        self.assertEqual(backend.calls, ["bookings"])

    async def test_mfa_prompt_is_exposed_only_through_authentication_status(self):
        service = AgentService(backend=Backend(listing()))
        self.addAsyncCleanup(service.close)
        prompted = asyncio.Event()

        async def run(kind, payload, progress, notify):
            await notify("PRIVATE_MFA")
            prompted.set()
            await asyncio.Event().wait()

        service.backend.run = run
        started = service.watch_booking(REF)
        job = service.get(started["operation_id"])
        await asyncio.wait_for(prompted.wait(), 2)
        self.assertEqual(service.authentication_status(job.id)["prompt"], "PRIVATE_MFA")
        self.assertNotIn("PRIVATE_MFA", str(service.result(job.id)))
        await service.cancel(job.id)
        self.assertIsNone(service.authentication_status(job.id)["prompt"])

    async def test_transient_backend_timeout_does_not_end_watch(self):
        _, job, _ = await self.run_watch(
            [asyncio.TimeoutError(), listing("Pending Approval")]
        )
        self.assertTrue(job.result["target_status_observed"])

    async def test_deadline_includes_wait_for_busy_browser_queue(self):
        backend = Backend(listing("Pending Approval"))
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        await service.queue.acquire()
        try:
            with patch(
                "sagasu.booking_watch.monotonic", side_effect=[0, 14.99, 14.99, 15]
            ):
                started = service.watch_booking(REF, timeout_seconds=15)
                job = service.get(started["operation_id"])
                await asyncio.wait_for(job.task, 2)
            self.assertEqual(job.result["outcome"], "timed_out")
            self.assertFalse(job.result["target_status_observed"])
            self.assertEqual(backend.calls, [])
        finally:
            service.queue.release()

    async def test_all_other_statuses_keep_waiting_without_ready_signal(self):
        for status in ("Confirmed", "Cancelled", "Rejected", "Withdrawn"):
            _, job, backend = await self.run_watch([listing(status)], timeout=15)
            self.assertFalse(job.result["target_status_observed"])
            self.assertEqual(job.result["outcome"], "timed_out")
            self.assertEqual(backend.calls, ["bookings"])

    async def test_progress_duplicate_cancellation_and_queue_release(self):
        backend = Backend(listing())
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        sleeping, resume = asyncio.Event(), asyncio.Event()

        async def sleep(seconds):
            sleeping.set()
            await resume.wait()

        with patch("sagasu.booking_watch.sleep", side_effect=sleep):
            started = service.watch_booking(REF)
            job = service.get(started["operation_id"])
            await asyncio.wait_for(sleeping.wait(), 2)
            duplicate = service.watch_booking(REF, timeout_seconds=60)
            self.assertEqual(duplicate["operation_id"], job.id)
            self.assertFalse(service.result(job.id)["result"]["target_status_observed"])
            self.assertEqual((await service.wait(job.id, 0))["status"], "running")
            other = service.start("bookings")
            self.assertEqual(
                (await service.wait(other["operation_id"], 2))["status"], "succeeded"
            )
            await service.cancel(job.id)
            self.assertEqual(job.result["outcome"], "cancelled")
            self.assertFalse(job.result["target_status_observed"])

    async def test_shutdown_stops_active_watcher(self):
        service = AgentService(backend=Backend(listing()))
        started = service.watch_booking(REF)
        await asyncio.sleep(0)
        await service.close()
        self.assertEqual(service.get(started["operation_id"]).status, "cancelled")

    async def test_watch_bounds_and_active_cap(self):
        service = AgentService(backend=Backend(listing()))
        self.addAsyncCleanup(service.close)
        for ref, interval, timeout in [
            ("", 30, 30),
            ("BK\n", 30, 30),
            (REF, True, 30),
            (REF, 14, 30),
            (REF, 301, 30),
            (REF, 30, 86401),
        ]:
            with self.assertRaises(AgentError):
                service.watch_booking(ref, interval, timeout)
        for i in range(4):
            service.watch_booking(f"BK-{i}")
        with self.assertRaises(AgentError):
            service.watch_booking("BK-5")

    async def test_mcp_contract_and_result(self):
        service = AgentService(backend=Backend(listing("Pending Approval")))
        self.addAsyncCleanup(service.close)
        async with Client(create_server(service)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            schema = tools["watch_booking"].input_schema["properties"]
            self.assertEqual(schema["interval_seconds"]["minimum"], 15)
            self.assertEqual(schema["timeout_seconds"]["maximum"], 86400)
            bad = await client.call_tool(
                "watch_booking", {"reference_number": REF, "interval_seconds": 1}
            )
            self.assertTrue(bad.is_error)
            self.assertEqual(service.jobs, {})
            started = (
                await client.call_tool("watch_booking", {"reference_number": REF})
            ).structured_content
            result = (
                await client.call_tool(
                    "wait_operation",
                    {"operation_id": started["operation_id"], "timeout": 2},
                )
            ).structured_content
            self.assertTrue(result["result"]["target_status_observed"])
