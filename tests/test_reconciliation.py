"""Uncertain writes need positive FBS evidence and cannot be retried via new reviews."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from mcp import Client
from sagasu.action_journal import ActionJournal, JournalError, action_summary
from sagasu.fbs_client import BookingOutcomeUnknown, BookingRequest, SearchConfig
from sagasu.mcp_service import AgentService, AgentError, Backend
from sagasu.mcp_server import create_server
from sagasu.reconciliation import reconcile
from sagasu.repeat import booking_window
from tests.test_mcp import ROW, CONFIG, TASK, FakeBackend, ServiceCase


class ReconciliationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def payload(self, kind="book", known=True):
        summary = {**ROW, **booking_window(ROW)}
        return {
            "kind": kind,
            "summary": summary,
            "reference_number": ROW["reference_number"] if known else None,
        }

    async def check(self, payload, rows, complete=True):
        with patch(
            "sagasu.reconciliation.read_bookings",
            AsyncMock(
                return_value={
                    "bookings": rows,
                    "total": len(rows) if complete else 100,
                    "complete": complete,
                }
            ),
        ):
            return await reconcile(Mock(), payload)

    async def test_exact_reference_confirmation_preserves_pending_status(self):
        for status, state in [
            ("Confirmed", "confirmed"),
            ("Pending Confirmation", "pending_confirmation"),
        ]:
            result = await self.check(self.payload(), [{**ROW, "fbs_status": status}])
            self.assertEqual(result["state"], state)
            self.assertTrue(result["action_outcome_verified"])
            self.assertEqual(result["fbs_status"], status)
            self.assertFalse(result["safe_to_retry"])

    async def test_absence_and_partial_coverage_do_not_prove_cancellation(self):
        for complete in (False, True):
            result = await self.check(self.payload("cancel"), [], complete)
            self.assertEqual(result["state"], "not_found")
            self.assertFalse(result["action_outcome_verified"])
            self.assertEqual(result["source_complete"], complete)

    async def test_positive_cancellation_evidence_is_required(self):
        for status, verified in [("Confirmed", False), ("Cancelled", True)]:
            result = await self.check(
                self.payload("cancel"), [{**ROW, "fbs_status": status}]
            )
            self.assertEqual(result["action_outcome_verified"], verified)
        wrong = {**ROW, "fbs_status": "Cancelled", "room": "Changed"}
        self.assertFalse(
            (await self.check(self.payload("cancel"), [wrong]))[
                "action_outcome_verified"
            ]
        )

    async def test_unknown_reference_and_ambiguous_matches_do_not_attribute_outcome(
        self,
    ):
        payload = self.payload(known=False)
        payload["summary"].pop("reference_number")
        result = await self.check(payload, [ROW])
        self.assertEqual(result["state"], "matching_booking_found")
        self.assertFalse(result["action_outcome_verified"])
        result = await self.check(
            payload, [ROW, {**ROW, "reference_number": "BK-OTHER"}]
        )
        self.assertEqual(result["state"], "ambiguous")
        wrong = {**ROW, "room": "Wrong room"}
        self.assertEqual(
            (await self.check(self.payload(), [wrong]))["state"], "unresolved"
        )

    async def test_approved_task_does_not_claim_confirmed_booking(self):
        for rows, state in [
            ([{**TASK, "status": "Approved"}], "approved"),
            ([], "not_found"),
            ([TASK], "unresolved"),
        ]:
            with patch(
                "sagasu.reconciliation.read_task_list",
                AsyncMock(return_value={"tasks": rows, "complete": True}),
            ):
                result = await reconcile(Mock(), self.payload("approve_task"))
            self.assertEqual(result["state"], state)
            if state == "approved":
                self.assertFalse(result["booking_status_verified"])

    async def test_backend_reconciliation_is_noninteractive_and_never_writes(self):
        client = Mock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.book = AsyncMock()
        with (
            patch("sagasu.mcp_service.FBSClient", return_value=client) as factory,
            patch(
                "sagasu.reconciliation.read_bookings",
                AsyncMock(return_value={"bookings": [ROW], "complete": True}),
            ),
        ):
            result = await Backend().run("reconcile", self.payload(), None, None)
        self.assertTrue(result["action_outcome_verified"])
        self.assertFalse(factory.call_args.kwargs["interactive"])
        client.book.assert_not_awaited()


class ReconciliationServiceTests(ServiceCase):
    async def unknown(self):
        review = await self.review()
        self.backend.error = BookingOutcomeUnknown("PRIVATE RAW RESPONSE")
        job = await self.finish(self.service.submit(review["review_id"]))
        self.backend.error = None
        return job

    async def test_new_review_cannot_bypass_unknown_action_or_ttl(self):
        original = await self.unknown()
        self.assertEqual(original.public()["next_tool"], "reconcile_operation")
        self.now += self.service.ttl + 1
        review = await self.review()
        with self.assertRaisesRegex(AgentError, original.id):
            self.service.submit(review["review_id"])
        self.assertEqual([k for k, _ in self.backend.calls].count("book"), 1)
        self.assertIn(review["review_id"], self.service.reviews)

    async def test_active_identical_action_blocks_second_review(self):
        first = await self.review()
        second = await self.review()
        self.backend.gate = __import__("asyncio").Event()
        operation = self.service.submit(first["review_id"])
        with self.assertRaises(AgentError):
            self.service.submit(second["review_id"])
        self.backend.gate.set()
        await self.finish(operation)

    async def test_mcp_reconciliation_persists_evidence_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ActionJournal(Path(directory) / "actions.sqlite3")
            self.service.journal = journal
            source = await self.unknown()
            evidence = {
                "state": "matching_booking_found",
                "reference_number": ROW["reference_number"],
                "fbs_status": "Confirmed",
                "action_outcome_verified": False,
                "safe_to_retry": False,
                "source_complete": True,
            }
            original_run = self.backend.run

            async def run(kind, payload, progress, notify):
                return (
                    copy.deepcopy(evidence)
                    if kind == "reconcile"
                    else await original_run(kind, payload, progress, notify)
                )

            self.backend.run = run
            async with Client(create_server(self.service)) as client:
                response = await client.call_tool(
                    "reconcile_operation", {"operation_id": source.id}
                )
                self.assertFalse(response.is_error)
                job = await self.finish(response.structured_content)
                self.assertEqual(job.result["state"], "matching_booking_found")
                self.assertFalse(job.result["safe_to_retry"])
            restarted = AgentService(
                backend=self.backend, journal=ActionJournal(journal.path)
            )
            self.addAsyncCleanup(restarted.close)
            recovered = restarted.get(source.id)
            self.assertEqual(
                recovered.reconciliation["state"], "matching_booking_found"
            )
            self.assertEqual(recovered.status, "outcome_unknown")
            self.assertTrue(recovered.public()["reconciliation_required"])
            # Even a newly prepared review after restart is blocked by durable history.
            search = await restarted.wait(restarted.search(CONFIG)["operation_id"])
            review = restarted.review_booking(
                search["operation_id"], ROW["room"], "Project work", "friend@smu.edu.sg"
            )
            with self.assertRaises(JournalError):
                restarted.submit(review["review_id"])
            self.assertNotIn("PRIVATE", json.dumps(journal.history()))

    async def test_verified_observation_does_not_rewrite_original_history(self):
        with tempfile.TemporaryDirectory() as directory:
            self.service.journal = ActionJournal(Path(directory) / "actions.sqlite3")
            source = await self.unknown()
            self.service.journal.record_reconciliation(
                source.id,
                {
                    "state": "confirmed",
                    "action_outcome_verified": True,
                    "reference_number": ROW["reference_number"],
                },
                self.now,
            )
            record = self.service.journal.get(source.id)
            self.assertEqual(record["status"], "outcome_unknown")
            self.assertFalse(record["reconciliation_required"])
            self.assertTrue(record["reconciliation"]["action_outcome_verified"])
