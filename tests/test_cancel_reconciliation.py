"""A cancellation that never took effect can be reconciled as "not_cancelled".

Live on 20 Nov 2026 a cancel whose outcome was uncertain blocked every retry,
because only a "Cancelled" row could verify it. Now an unchanged row, well
after the attempt, proves it did not take effect, and only then is a retry
safe. Stand-in listings only; never live FBS.
"""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sagasu.action_journal import ActionJournal, JournalError, action_summary
from sagasu.fbs_account import CancellationOutcomeUnknown
from sagasu.fbs_client import BookingOutcomeUnknown
from sagasu.mcp_service import AgentError, Job
from sagasu.reconciliation import CANCEL_SETTLE_SECONDS, evidence, reconcile
from tests.test_mcp import ROW, ServiceCase

REF = ROW["reference_number"]
# What the service stores for a cancel: the reviewed row's safe fields.
SUMMARY = action_summary("cancel", (copy.deepcopy(ROW), "private reason"))
VERIFIED_STATES = {
    "confirmed",
    "pending_confirmation",
    "cancelled",
    "not_cancelled",
    "approved",
    "amended",
}
OTHER_STATES = {"matching_booking_found", "not_found", "unresolved", "ambiguous"}


def listing(rows, complete=True):
    return {
        "bookings": copy.deepcopy(rows),
        "total": len(rows) if complete else len(rows) + 50,
        "complete": complete,
    }


def payload(kind="cancel", summary=None, **extra):
    data = {
        "operation_id": "op-original",
        "kind": kind,
        "summary": copy.deepcopy(SUMMARY if summary is None else summary),
        "reference_number": REF,
        "settled": True,
    }
    data.update(extra)
    return data


async def run_reconcile(data, rows, complete=True):
    """reconcile() against a stand-in listing; the client is never touched."""
    client = Mock()
    with (
        patch(
            "sagasu.reconciliation.read_bookings",
            AsyncMock(return_value=listing(rows, complete)),
        ),
        patch(
            "sagasu.booking_details.read_booking_details",
            AsyncMock(return_value={"reference_verified": False}),
        ),
    ):
        return await reconcile(client, data)


class SummaryShapeTests(unittest.TestCase):
    def test_cancel_summary_keeps_room_time_and_status_for_comparison(self):
        self.assertEqual(SUMMARY["reference_number"], REF)
        self.assertEqual(SUMMARY["room"], ROW["room"])
        self.assertEqual(SUMMARY["date_time"], ROW["date_time"])
        self.assertEqual(SUMMARY["fbs_status"], ROW["fbs_status"])

    def test_settle_time_is_fifteen_minutes(self):
        self.assertEqual(CANCEL_SETTLE_SECONDS, 15 * 60)


class NotCancelledEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def assertNotCancelled(self, result, status=ROW["fbs_status"]):
        self.assertEqual(result["state"], "not_cancelled")
        self.assertIs(result["action_outcome_verified"], True)
        self.assertIs(result["safe_to_retry"], True)
        self.assertEqual(result["reference_number"], REF)
        self.assertEqual(result["fbs_status"], status)
        self.assertIs(result["source_complete"], True)
        self.assertIn("did not take effect", result["message"])

    def assertRefused(self, result):
        self.assertNotEqual(result["state"], "not_cancelled")
        self.assertIs(result["action_outcome_verified"], False)
        self.assertIs(result["safe_to_retry"], False)

    async def test_unchanged_row_after_settling_is_not_cancelled(self):
        self.assertNotCancelled(await run_reconcile(payload(), [ROW]))

    async def test_other_rows_in_the_listing_do_not_matter(self):
        other = {**ROW, "reference_number": "BK-20991101-000999", "room": "Other"}
        self.assertNotCancelled(await run_reconcile(payload(), [other, ROW, other]))

    async def test_status_comparison_ignores_case_and_surrounding_space(self):
        row = {**ROW, "fbs_status": "  confirmed "}
        self.assertNotCancelled(await run_reconcile(payload(), [row]), "  confirmed ")
        summary = {**SUMMARY, "fbs_status": "Pending Confirmation"}
        row = {**ROW, "fbs_status": "PENDING CONFIRMATION"}
        self.assertNotCancelled(
            await run_reconcile(payload(summary=summary), [row]),
            "PENDING CONFIRMATION",
        )

    async def test_each_broken_condition_refuses_not_cancelled(self):
        two = [ROW, copy.deepcopy(ROW)]
        no_room = {k: v for k, v in SUMMARY.items() if k != "room"}
        no_time = {k: v for k, v in SUMMARY.items() if k != "date_time"}
        no_status = {k: v for k, v in SUMMARY.items() if k != "fbs_status"}
        cases = [
            ("not settled", payload(settled=False), [ROW], True),
            (
                "settled missing",
                {k: v for k, v in payload().items() if k != "settled"},
                [ROW],
                True,
            ),
            ("settled 1", payload(settled=1), [ROW], True),
            ("settled 'true'", payload(settled="true"), [ROW], True),
            ("settled None", payload(settled=None), [ROW], True),
            ("incomplete listing", payload(), [ROW], False),
            (
                "status moved on",
                payload(),
                [{**ROW, "fbs_status": "Pending Confirmation"}],
                True,
            ),
            (
                "status moved back",
                payload(summary={**SUMMARY, "fbs_status": "Pending Confirmation"}),
                [ROW],
                True,
            ),
            (
                "pending cancellation, unchanged",
                payload(summary={**SUMMARY, "fbs_status": "Pending Cancellation"}),
                [{**ROW, "fbs_status": "Pending Cancellation"}],
                True,
            ),
            (
                "cancellation requested, unchanged",
                payload(summary={**SUMMARY, "fbs_status": "Cancellation Requested"}),
                [{**ROW, "fbs_status": "Cancellation Requested"}],
                True,
            ),
            ("room differs", payload(), [{**ROW, "room": "SCIS1 GSR 2-2"}], True),
            (
                "room differs only by space",
                payload(),
                [{**ROW, "room": ROW["room"] + " "}],
                True,
            ),
            (
                "date_time differs",
                payload(),
                [{**ROW, "date_time": "01-Nov-2099 (Sun) 16:00 - 18:00 (2hrs)"}],
                True,
            ),
            ("row lacks room", payload(), [{**ROW, "room": None}], True),
            ("summary lacks room", payload(summary=no_room), [ROW], True),
            ("summary lacks date_time", payload(summary=no_time), [ROW], True),
            (
                "summary and row both empty room",
                payload(summary={**SUMMARY, "room": ""}),
                [{**ROW, "room": ""}],
                True,
            ),
            (
                "summary and row both empty date_time",
                payload(summary={**SUMMARY, "date_time": ""}),
                [{**ROW, "date_time": ""}],
                True,
            ),
            ("summary lacks fbs_status", payload(summary=no_status), [ROW], True),
            (
                "summary fbs_status not a string",
                payload(summary={**SUMMARY, "fbs_status": None}),
                [ROW],
                True,
            ),
            ("two matching rows", payload(), two, True),
            ("reference absent", payload(), [], True),
            (
                "reference differs only by case",
                payload(),
                [{**ROW, "reference_number": REF.lower()}],
                True,
            ),
            ("kind book", payload(kind="book"), [ROW], True),
            ("kind amend", payload(kind="amend"), [ROW], True),
        ]
        for label, data, rows, complete in cases:
            with self.subTest(label):
                self.assertRefused(await run_reconcile(data, rows, complete))

    async def test_book_and_amend_never_reach_not_cancelled_with_any_status(self):
        for kind in ("book", "amend"):
            for status in ("Confirmed", "Pending Approval", "Rejected"):
                with self.subTest(kind=kind, status=status):
                    result = await run_reconcile(
                        payload(kind=kind, summary={**SUMMARY, "fbs_status": status}),
                        [{**ROW, "fbs_status": status}],
                    )
                    self.assertNotEqual(result["state"], "not_cancelled")
                    self.assertIs(result["safe_to_retry"], False)

    async def test_cancelled_row_keeps_the_verified_cancelled_state(self):
        for settled in (True, False):
            for status in ("Cancelled", "canceled", " CANCELLED "):
                with self.subTest(settled=settled, status=status):
                    result = await run_reconcile(
                        payload(settled=settled), [{**ROW, "fbs_status": status}]
                    )
                    self.assertEqual(result["state"], "cancelled")
                    self.assertIs(result["action_outcome_verified"], True)
                    self.assertIs(result["safe_to_retry"], False)

    async def test_cancelled_row_with_changed_room_is_still_unresolved(self):
        result = await run_reconcile(
            payload(), [{**ROW, "fbs_status": "Cancelled", "room": "Elsewhere"}]
        )
        self.assertEqual(result["state"], "unresolved")
        self.assertIs(result["safe_to_retry"], False)

    async def test_only_the_reconciliation_listing_is_read(self):
        client = Mock()
        reads = AsyncMock(return_value=listing([ROW]))
        with patch("sagasu.reconciliation.read_bookings", reads):
            await reconcile(client, payload())
        reads.assert_awaited_once_with(client)
        # A Mock records every attribute touched; nothing on it was called.
        self.assertEqual(client.method_calls, [])


class JournalNotCancelledTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = ActionJournal(Path(temporary.name) / "private/actions.sqlite3")

    def unknown_cancel(self, operation_id="op-original", at=100.0):
        job = Job(operation_id, "cancel", at, updated_at=at)
        self.journal.reserve(job, "review-" + operation_id, SUMMARY)
        job.status, job.updated_at = "running", at + 1
        self.journal.update(job)
        job.status, job.updated_at = "outcome_unknown", at + 2
        job.error = {"code": "CancellationOutcomeUnknown"}
        self.journal.update(job)
        return job

    def reserve_retry(self, operation_id="op-retry"):
        job = Job(operation_id, "cancel", 500.0, updated_at=500.0)
        self.journal.reserve(job, "review-" + operation_id, SUMMARY)
        return job

    def not_cancelled(self):
        return asyncio.run(run_reconcile(payload(), [ROW]))

    def test_unknown_cancel_blocks_retry_until_not_cancelled_is_recorded(self):
        original = self.unknown_cancel()
        with self.assertRaisesRegex(JournalError, original.id):
            self.reserve_retry()
        self.assertEqual(self.journal.unresolved_count(), 1)
        stored = self.journal.record_reconciliation(
            original.id, self.not_cancelled(), 1000.0
        )
        self.assertEqual(stored["state"], "not_cancelled")
        self.assertIs(stored["action_outcome_verified"], True)
        self.assertIs(stored["safe_to_retry"], True)
        self.assertEqual(stored["observed_at"], 1000.0)
        record = self.journal.get(original.id)
        self.assertEqual(record["status"], "outcome_unknown")
        self.assertFalse(record["reconciliation_required"])
        self.assertIs(record["reconciliation"]["safe_to_retry"], True)
        self.assertEqual(self.journal.unresolved_count(), 0)
        retry = self.reserve_retry()
        self.assertEqual(self.journal.get(retry.id)["status"], "queued")

    def test_not_cancelled_counts_only_for_a_cancel(self):
        book = {
            "room": "Room A",
            "date": "01-Jan-2099",
            "start_time": "15:00",
            "end_time": "17:00",
        }
        job = Job("op-book", "book", 1.0, updated_at=1.0)
        self.journal.reserve(job, "review-book", book)
        job.status = "outcome_unknown"
        self.journal.update(job)
        stored = self.journal.record_reconciliation(job.id, self.not_cancelled(), 2.0)
        self.assertIs(stored["action_outcome_verified"], False)
        self.assertIs(stored["safe_to_retry"], False)
        with self.assertRaises(JournalError):
            self.journal.reserve(Job("op-book-2", "book", 3.0), "review-book-2", book)

    def test_unknown_cancel_for_another_reference_never_blocks(self):
        self.unknown_cancel()
        other = {**SUMMARY, "reference_number": "BK-20991101-000999"}
        self.journal.reserve(Job("op-other", "cancel", 5.0), "review-other", other)

    def test_running_cancel_blocks_even_with_not_cancelled_evidence(self):
        job = Job("op-running", "cancel", 1.0, updated_at=1.0)
        self.journal.reserve(job, "review-running", SUMMARY)
        job.status = "running"
        self.journal.update(job)
        self.journal.record_reconciliation(job.id, self.not_cancelled(), 2.0)
        with self.assertRaises(JournalError):
            self.reserve_retry()

    def test_caller_cannot_assert_verification_or_retry_for_unresolved(self):
        original = self.unknown_cancel()
        stored = self.journal.record_reconciliation(
            original.id,
            {
                "state": "unresolved",
                "action_outcome_verified": True,
                "safe_to_retry": True,
            },
            1000.0,
        )
        self.assertIs(stored["action_outcome_verified"], False)
        self.assertIs(stored["safe_to_retry"], False)
        saved = self.journal.get(original.id)["reconciliation"]
        self.assertIs(saved["action_outcome_verified"], False)
        self.assertIs(saved["safe_to_retry"], False)
        with self.assertRaises(JournalError):
            self.reserve_retry()

    def test_cancelled_is_verified_but_never_safe_to_retry(self):
        original = self.unknown_cancel()
        stored = self.journal.record_reconciliation(
            original.id,
            {
                "state": "cancelled",
                "action_outcome_verified": True,
                "safe_to_retry": True,
                "reference_number": REF,
                "fbs_status": "Cancelled",
            },
            1000.0,
        )
        self.assertIs(stored["action_outcome_verified"], True)
        self.assertIs(stored["safe_to_retry"], False)
        self.assertIs(
            self.journal.get(original.id)["reconciliation"]["safe_to_retry"], False
        )

    def test_every_state_but_verified_not_cancelled_stores_no_retry(self):
        original = self.unknown_cancel()
        for state in sorted((VERIFIED_STATES | OTHER_STATES) - {"not_cancelled"}):
            with self.subTest(state=state):
                stored = self.journal.record_reconciliation(
                    original.id,
                    {
                        "state": state,
                        "action_outcome_verified": True,
                        "safe_to_retry": True,
                    },
                    1000.0,
                )
                self.assertIs(
                    stored["action_outcome_verified"], state in VERIFIED_STATES
                )
                self.assertIs(stored["safe_to_retry"], False)
                self.assertIs(
                    self.journal.get(original.id)["reconciliation"]["safe_to_retry"],
                    False,
                )

    def test_not_cancelled_needs_true_flags_not_truthy_ones(self):
        original = self.unknown_cancel()
        for flags, verified, retry in (
            ({"action_outcome_verified": False, "safe_to_retry": True}, False, False),
            ({"safe_to_retry": True}, False, False),
            ({"action_outcome_verified": "true", "safe_to_retry": True}, False, False),
            ({"action_outcome_verified": 1, "safe_to_retry": True}, False, False),
            ({"action_outcome_verified": True}, True, False),
            ({"action_outcome_verified": True, "safe_to_retry": 1}, True, False),
            ({"action_outcome_verified": True, "safe_to_retry": "yes"}, True, False),
            ({"action_outcome_verified": True, "safe_to_retry": True}, True, True),
        ):
            with self.subTest(flags=flags):
                stored = self.journal.record_reconciliation(
                    original.id, {"state": "not_cancelled", **flags}, 1000.0
                )
                self.assertIs(stored["action_outcome_verified"], verified)
                self.assertIs(stored["safe_to_retry"], retry)

    def test_unverified_not_cancelled_keeps_the_block(self):
        original = self.unknown_cancel()
        self.journal.record_reconciliation(
            original.id,
            {"state": "not_cancelled", "action_outcome_verified": False},
            1000.0,
        )
        with self.assertRaises(JournalError):
            self.reserve_retry()

    def test_later_unresolved_evidence_replaces_and_restores_the_block(self):
        original = self.unknown_cancel()
        self.journal.record_reconciliation(original.id, self.not_cancelled(), 1000.0)
        later = asyncio.run(run_reconcile(payload(), [ROW], complete=False))
        self.assertEqual(later["state"], "unresolved")
        stored = self.journal.record_reconciliation(original.id, later, 2000.0)
        self.assertIs(stored["safe_to_retry"], False)
        self.assertTrue(self.journal.get(original.id)["reconciliation_required"])
        with self.assertRaises(JournalError):
            self.reserve_retry()

    def test_unknown_or_misspelt_states_are_rejected(self):
        original = self.unknown_cancel()
        for state in ("NOT_CANCELLED", "not_cancelled ", "not-cancelled", None):
            with self.subTest(state=state):
                with self.assertRaises(JournalError):
                    self.journal.record_reconciliation(
                        original.id,
                        {
                            "state": state,
                            "action_outcome_verified": True,
                            "safe_to_retry": True,
                        },
                        1000.0,
                    )
        self.assertIsNone(self.journal.get(original.id)["reconciliation"])

    def test_not_cancelled_evidence_round_trips_through_reconcile_helper(self):
        built = evidence(
            "not_cancelled",
            verified=True,
            reference=REF,
            status="Confirmed",
            complete=True,
            retry=True,
        )
        self.assertEqual(built, self.not_cancelled())


class ReconcileOperationSettleTests(ServiceCase):
    """reconcile_operation tells reconcile() whether a cancel has settled."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reconcile_payloads = []
        self.listing = [copy.deepcopy(ROW)]
        original_run = self.backend.run

        async def run(kind, data, progress, notify):
            if kind != "reconcile":
                return await original_run(kind, data, progress, notify)
            self.backend.calls.append((kind, data))
            self.reconcile_payloads.append(copy.deepcopy(data))
            with (
                patch(
                    "sagasu.reconciliation.read_bookings",
                    AsyncMock(return_value=listing(self.listing)),
                ),
                patch(
                    "sagasu.reconciliation.read_task_list",
                    AsyncMock(return_value={"tasks": [], "complete": True}),
                ),
                patch(
                    "sagasu.booking_details.read_booking_details",
                    AsyncMock(return_value={"reference_verified": False}),
                ),
            ):
                return await reconcile(Mock(), data)

        self.backend.run = run

    def use_journal(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service.journal = ActionJournal(
            Path(temporary.name) / "private/actions.sqlite3"
        )

    async def cancel_review(self):
        source = await self.finish(self.service.start("bookings"))
        return self.service.review_cancellation(source.id, REF, "Not needed")

    async def unknown_cancel(self):
        review = await self.cancel_review()
        self.backend.error = CancellationOutcomeUnknown("PRIVATE RAW PAGE")
        job = await self.finish(self.service.submit(review["review_id"]))
        self.backend.error = None
        self.assertEqual(job.status, "outcome_unknown")
        return job

    async def reconcile_at(self, operation_id, now):
        self.now = now
        job = await self.finish(self.service.reconcile_operation(operation_id))
        self.assertEqual(job.status, "succeeded", job.error)
        return self.reconcile_payloads[-1], job

    async def test_settled_follows_the_clock_from_the_attempt(self):
        original = await self.unknown_cancel()
        attempted = original.updated_at
        self.assertEqual(attempted, self.now)
        for elapsed, settled in (
            (0, False),
            (60, False),
            (CANCEL_SETTLE_SECONDS - 1, False),
            (CANCEL_SETTLE_SECONDS, True),
            (CANCEL_SETTLE_SECONDS + 1, True),
            # Still inside the in-memory TTL, so the original stays listed.
            (2 * CANCEL_SETTLE_SECONDS, True),
        ):
            with self.subTest(elapsed=elapsed):
                sent, job = await self.reconcile_at(original.id, attempted + elapsed)
                self.assertIs(sent["settled"], settled)
                self.assertEqual(sent["kind"], "cancel")
                self.assertEqual(sent["reference_number"], REF)
                self.assertEqual(
                    job.result["state"], "not_cancelled" if settled else "unresolved"
                )

    async def test_settled_uses_the_journal_time_after_memory_expires(self):
        self.use_journal()
        original = await self.unknown_cancel()
        attempted = original.updated_at
        self.now = attempted + self.service.ttl + 1
        self.service.prune()
        self.assertNotIn(original.id, self.service.jobs)
        sent, job = await self.reconcile_at(original.id, self.now)
        self.assertIs(sent["settled"], True)
        self.assertEqual(job.result["state"], "not_cancelled")
        self.assertIs(
            self.service.journal.get(original.id)["reconciliation"]["safe_to_retry"],
            True,
        )

    async def test_non_cancel_kinds_send_no_settled_flag(self):
        for kind in ("book", "approve_task", "amend"):
            with self.subTest(kind=kind):
                operation_id = "op-" + kind
                self.service.jobs[operation_id] = Job(
                    operation_id,
                    kind,
                    self.now,
                    status="outcome_unknown",
                    updated_at=self.now,
                    summary={
                        "reference_number": REF,
                        "room": ROW["room"],
                        "date": "01-Nov-2099",
                        "start_time": "14:00",
                        "end_time": "16:00",
                        "date_time": ROW["date_time"],
                        "fbs_status": "Confirmed",
                    },
                )
                self.now += 10 * CANCEL_SETTLE_SECONDS
                job = self.service.reconcile_operation(operation_id)
                await self.service.get(job["operation_id"]).task
                sent = self.reconcile_payloads[-1]
                self.assertEqual(sent["kind"], kind)
                self.assertNotIn("settled", sent)
                result = self.service.get(job["operation_id"]).result or {}
                self.assertNotEqual(result.get("state"), "not_cancelled")

    async def test_unknown_booking_reconciliation_sends_no_settled_flag(self):
        review = await self.review()
        self.backend.error = BookingOutcomeUnknown("PRIVATE RAW RESPONSE")
        original = await self.finish(self.service.submit(review["review_id"]))
        self.backend.error = None
        self.assertEqual(original.status, "outcome_unknown")
        sent, _ = await self.reconcile_at(
            original.id, original.updated_at + 10 * CANCEL_SETTLE_SECONDS
        )
        self.assertEqual(sent["kind"], "book")
        self.assertNotIn("settled", sent)

    async def test_retry_is_blocked_until_settled_not_cancelled_then_allowed(self):
        self.use_journal()
        original = await self.unknown_cancel()
        attempted = original.updated_at
        # Too early: unchanged row proves nothing yet; retries stay blocked.
        _, early = await self.reconcile_at(original.id, attempted + 60)
        self.assertEqual(early.result["state"], "unresolved")
        self.assertIs(early.result["safe_to_retry"], False)
        review = await self.cancel_review()
        with self.assertRaisesRegex((AgentError, JournalError), original.id):
            self.service.submit(review["review_id"])
        # Settled and unchanged: the cancel did not take effect.
        _, late = await self.reconcile_at(
            original.id, attempted + CANCEL_SETTLE_SECONDS
        )
        self.assertEqual(late.result["state"], "not_cancelled")
        self.assertIs(late.result["safe_to_retry"], True)
        self.assertEqual(late.result["original_operation_id"], original.id)
        self.assertFalse(
            self.service.get(original.id).public()["reconciliation_required"]
        )
        review = await self.cancel_review()
        retry = await self.finish(self.service.submit(review["review_id"]))
        self.assertEqual(retry.status, "succeeded")
        self.assertEqual([kind for kind, _ in self.backend.calls].count("cancel"), 2)

    async def test_changed_row_after_settling_keeps_retry_blocked(self):
        self.use_journal()
        original = await self.unknown_cancel()
        self.listing = [{**ROW, "fbs_status": "Pending Cancellation"}]
        _, job = await self.reconcile_at(
            original.id, original.updated_at + CANCEL_SETTLE_SECONDS
        )
        self.assertEqual(job.result["state"], "unresolved")
        self.assertTrue(
            self.service.journal.get(original.id)["reconciliation_required"]
        )
        self.listing = [copy.deepcopy(ROW)]
        review = await self.cancel_review()
        with self.assertRaises((AgentError, JournalError)):
            self.service.submit(review["review_id"])
        self.assertEqual([kind for kind, _ in self.backend.calls].count("cancel"), 1)


if __name__ == "__main__":
    unittest.main()
