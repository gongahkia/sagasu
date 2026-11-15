"""Native amendments, reference binding, previews, response loss and recovery."""

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mcp import Client
from sagasu.action_journal import ActionJournal, JournalError, action_summary
from sagasu.amendments import (
    AmendmentUnavailable,
    AmendmentOutcomeUnknown,
    additional_intervals,
    check_added_intervals,
    edit_snapshot,
    execute_amendment,
    matches_digest,
    open_edit,
    prepare_amendment,
    read_amendment_options,
    set_edit_date,
    verification_digest,
)
from sagasu.fbs_client import (
    FBS_TIMEZONE,
    LayoutChanged,
    RoomAvailability,
    SearchResult,
    TimeSlot,
)
from sagasu.mcp_server import create_server
from sagasu.mcp_service import AgentError, AgentService, Backend
from tests.amendment_fixture import AmendmentFixture, SOURCE

KEY = b"k" * 32


def options():
    return {
        "available": True,
        "current": copy.deepcopy(SOURCE),
        "supported_changes": [
            "date",
            "start_time",
            "end_time",
            "purpose",
            "usage_type",
            "booking_usage",
        ],
        "usage_types": ["AdHoc", "Academic"],
        "booking_usages": ["Meeting", "Research"],
    }


class AmendmentPlanningTests(unittest.TestCase):
    def test_delta_checks_only_added_time_and_preserves_verified_room_scope(self):
        payload = prepare_amendment(
            options(), {"start_time": "13:00", "end_time": "17:00"}, KEY
        )
        extra = additional_intervals(payload)
        self.assertEqual(
            [(c.start_time, c.end_time) for c in extra],
            [("13:00", "14:00"), ("16:00", "17:00")],
        )
        self.assertTrue(
            all(
                c.buildings == ("Li Ka Shing Library",)
                and c.facility_types == ("Project Room (Level 5)",)
                for c in extra
            )
        )
        self.assertEqual(
            additional_intervals(
                prepare_amendment(options(), {"end_time": "15:00"}, KEY)
            ),
            [],
        )
        moved = additional_intervals(
            prepare_amendment(options(), {"date": "2099-11-02"}, KEY)
        )
        self.assertEqual(
            [(c.date, c.start_time, c.end_time) for c in moved],
            [("02-Nov-2099", "14:00", "16:00")],
        )

    def test_ongoing_extensions_are_allowed_but_past_new_starts_and_ends_are_rejected(
        self,
    ):
        now = datetime(2099, 11, 1, 14, 30, tzinfo=FBS_TIMEZONE)
        self.assertEqual(
            prepare_amendment(options(), {"end_time": "17:00"}, KEY, now=now)["target"][
                "start_time"
            ],
            "14:00",
        )
        for changes in [
            {"start_time": "14:00", "end_time": "14:30"},
            {"start_time": "13:30"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(AmendmentUnavailable):
                prepare_amendment(options(), changes, KEY, now=now)

    def test_unavailable_immutable_null_or_unsupported_fields_never_create_requests(
        self,
    ):
        for changes in [
            {},
            {"room": "Room B"},
            {"participants": []},
            {"purpose": None},
            {"purpose": ""},
            {"booking_usage": "Unknown"},
            {"purpose": "two\nlines"},
            {"end_time": "14:00"},
            {"purpose": "Original purpose"},
        ]:
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, AmendmentUnavailable)),
            ):
                prepare_amendment(options(), changes, KEY)
        with self.assertRaises(ValueError):
            prepare_amendment({"available": False}, {"purpose": "New"}, KEY)

    def test_comparison_key_survives_restart_without_storing_purpose_in_action_summary(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "actions.sqlite3"
            journal = ActionJournal(path)
            key = journal.comparison_key()
            self.assertEqual(ActionJournal(path).comparison_key(), key)
            payload = prepare_amendment(options(), {"purpose": "PRIVATE PURPOSE"}, key)
            summary = action_summary("amend", payload)
            self.assertNotIn("PRIVATE PURPOSE", json.dumps(summary))
            self.assertTrue(
                matches_digest(key, summary, {"purpose": "PRIVATE PURPOSE"})
            )
            self.assertFalse(matches_digest(key, summary, {"purpose": "Other purpose"}))
            self.assertFalse(
                matches_digest(
                    key,
                    {**summary, "verification_digest": "invalid"},
                    {"purpose": "PRIVATE PURPOSE"},
                )
            )
            with journal._db() as db:
                db.execute("UPDATE comparison_metadata SET value='broken'")
            with self.assertRaises(JournalError):
                ActionJournal(path).comparison_key()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class AmendmentBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = AmendmentFixture()
        await self.fixture.setup(self)

    async def payload(self, changes):
        native = await read_amendment_options(self.fixture.client, "BK-TEST")
        return prepare_amendment(native, changes, KEY)

    async def test_reference_is_bound_to_exact_native_action_even_without_an_edit_reference_field(
        self,
    ):
        native = await read_amendment_options(self.fixture.client, "BK-TEST")
        self.assertTrue(native["available"])
        self.assertEqual(native["current"]["reference_number"], "BK-TEST")
        self.assertIn("omits the reference", native["reference_binding"])
        self.assertEqual(self.fixture.actions, ["EDIT"])
        self.assertEqual(self.fixture.posts, 0)
        self.fixture.offered = False
        native = await read_amendment_options(self.fixture.client, "BK-TEST")
        self.assertFalse(native["native_edit_offered"])

    async def test_partial_or_ambiguous_source_listing_and_wrong_edit_identity_fail_closed(
        self,
    ):
        self.fixture.complete = False
        with self.assertRaises(AmendmentUnavailable):
            await read_amendment_options(self.fixture.client, "BK-TEST")
        self.fixture.complete = True
        self.fixture.extra = [
            {**self.fixture.row(SOURCE), "reference_number": "BK-DUPLICATE"}
        ]
        with self.assertRaises(AmendmentUnavailable):
            await read_amendment_options(self.fixture.client, "BK-TEST")
        self.assertEqual(self.fixture.actions, [])
        self.fixture.extra = []
        self.fixture.form_overrides = {"room": "Wrong room"}
        with self.assertRaises(LayoutChanged):
            await read_amendment_options(self.fixture.client, "BK-TEST")
        self.assertEqual(self.fixture.posts, 0)

    async def test_negative_reconciliation_does_not_mark_an_unapplied_goal_verified(
        self,
    ):
        from sagasu.reconciliation import reconcile

        payload = await self.payload({"purpose": "Requested new purpose"})
        summary = action_summary("amend", payload)
        evidence = await reconcile(
            self.fixture.client,
            {
                "kind": "amend",
                "summary": summary,
                "reference_number": "BK-TEST",
                "verification_key": KEY,
            },
        )
        self.assertEqual(evidence["state"], "unresolved")
        self.assertFalse(evidence["action_outcome_verified"])
        self.assertFalse(evidence["safe_to_retry"])
        self.assertEqual(self.fixture.posts, 0)

    async def test_full_preview_changes_only_draft_and_reads_native_recalculated_credit_estimate(
        self,
    ):
        before = copy.deepcopy(self.fixture.saved)
        payload = await self.payload(
            {
                "end_time": "15:30",
                "purpose": "Changed purpose",
                "booking_usage": "Research",
            }
        )
        result = await execute_amendment(
            payload,
            submit=False,
            progress=AsyncMock(),
            auth_notify=AsyncMock(),
            interactive=False,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["estimated_credits"], "21.00")
        self.assertEqual(result["participant_count"], 1)
        self.assertEqual(self.fixture.saved, before)
        self.assertEqual(self.fixture.posts, 0)
        self.assertNotIn("CONFIRM", self.fixture.actions)

    async def test_native_calendar_crosses_month_and_year_without_filling_readonly_date_input(
        self,
    ):
        frame, record = await open_edit(self.fixture.client, "BK-TEST")
        await set_edit_date(self.fixture.client, frame, "01-Jan-2100")
        data = await edit_snapshot(frame, record, verify_original=False)
        self.assertEqual(data["current"]["date"], "01-Jan-2100")
        self.assertIn("DATE", self.fixture.actions)
        self.assertEqual(self.fixture.posts, 0)

    async def test_changed_source_or_lost_participant_is_detected_before_confirmation(
        self,
    ):
        payload = await self.payload(
            {"purpose": "Changed purpose", "booking_usage": "Research"}
        )
        self.fixture.saved["purpose"] = "Changed by someone else"
        with self.assertRaises(AmendmentUnavailable):
            await execute_amendment(
                payload, submit=True, progress=AsyncMock(), auth_notify=AsyncMock()
            )
        self.assertEqual(self.fixture.posts, 0)
        self.fixture.saved = copy.deepcopy(SOURCE)
        self.fixture.lose_participant = True
        with self.assertRaises(AmendmentUnavailable):
            await execute_amendment(
                payload, submit=True, progress=AsyncMock(), auth_notify=AsyncMock()
            )
        self.assertEqual(self.fixture.posts, 0)

    async def test_fixture_submission_verifies_exact_requested_fields_and_keeps_pending_status(
        self,
    ):
        payload = await self.payload({"purpose": "Changed purpose"})
        with (
            patch(
                "sagasu.amendments.capture_booking_evidence",
                AsyncMock(side_effect=RuntimeError("PRIVATE RECEIPT ERROR")),
            ),
            patch("sagasu.amendments.checkpoint_verified") as checkpoint,
        ):
            result = await execute_amendment(
                payload, submit=True, progress=AsyncMock(), auth_notify=AsyncMock()
            )
        self.assertEqual(result["status"], "verified_amendment")
        self.assertEqual(result["fbs_status"], "Pending Confirmation")
        self.assertEqual(self.fixture.posts, 1)
        self.assertNotIn("PRIVATE RECEIPT ERROR", json.dumps(result))
        checkpoint.assert_called_once()

    async def test_lost_submit_response_is_unknown_and_reconciliation_after_restart_checks_private_goal(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            journal = ActionJournal(Path(temporary) / "actions.sqlite3")
            backend = Backend(interactive_auth=False)
            service = AgentService(backend=backend, journal=journal)
            self.addAsyncCleanup(service.close)
            native = service.amendment_options("BK-TEST")
            await service.get(native["operation_id"]).task
            review = service.review_amendment(
                native["operation_id"], {"purpose": "PRIVATE PURPOSE"}
            )
            self.fixture.drop_response = True
            with patch("sagasu.amendments.CONFIRM_TIMEOUT_MS", 1000):
                submitted = service.submit(review["review_id"])
                original = service.get(submitted["operation_id"])
                await original.task
            self.assertEqual(original.status, "outcome_unknown")
            self.assertEqual(self.fixture.posts, 1)
            self.assertNotIn("PRIVATE PURPOSE", json.dumps(journal.history()))
            await service.close()
            restarted = AgentService(
                backend=backend, journal=ActionJournal(journal.path)
            )
            self.addAsyncCleanup(restarted.close)
            reconciliation = restarted.reconcile_operation(original.id)
            checked = restarted.get(reconciliation["operation_id"])
            await checked.task
            self.assertEqual(checked.status, "succeeded")
            self.assertEqual(checked.result["state"], "amended")
            self.assertTrue(
                restarted.get(original.id).reconciliation["action_outcome_verified"]
            )
            self.assertEqual(self.fixture.posts, 1)
            with journal._db() as db:
                db.execute("DELETE FROM comparison_metadata")
            with self.assertRaises(JournalError):
                ActionJournal(journal.path).comparison_key()

    async def test_mcp_review_and_validation_preserve_exact_native_target_and_never_submit(
        self,
    ):
        service = AgentService(backend=Backend(interactive_auth=False))
        self.addAsyncCleanup(service.close)
        async with Client(create_server(service)) as client:
            native = await client.call_tool(
                "amendment_options", {"reference_number": "BK-TEST"}
            )
            operation = service.get(native.structured_content["operation_id"])
            await operation.task
            review = await client.call_tool(
                "amendment_review",
                {
                    "operation_id": operation.id,
                    "changes": {"purpose": "Changed purpose"},
                },
            )
            self.assertFalse(review.is_error)
            self.assertEqual(
                review.structured_content["summary"]["reference_number"], "BK-TEST"
            )
            validation = await client.call_tool(
                "validate_booking",
                {"review_id": review.structured_content["review_id"]},
            )
            job = service.get(validation.structured_content["operation_id"])
            await job.task
            self.assertEqual(job.status, "succeeded")
        self.assertEqual(self.fixture.posts, 0)


class AmendmentAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_added_interval_checks_use_hybrid_search_before_opening_a_write_browser(
        self,
    ):
        backend = Backend(interactive_auth=False)
        backend.reads.mode = "api"
        calls = []

        async def read(kind, config, progress):
            calls.append((kind, config.start_time, config.end_time))
            return SearchResult(
                config,
                [
                    RoomAvailability(
                        "Room A", [TimeSlot(config.start_time, config.end_time, "free")]
                    )
                ],
                "fixture",
            ).to_dict()

        backend.reads.direct = AsyncMock(side_effect=read)
        payload = prepare_amendment(options(), {"end_time": "17:00"}, KEY)

        async def execute(*args, **kwargs):
            self.assertEqual(calls, [("search", "16:00", "17:00")])
            self.assertFalse(kwargs["submit"])
            return {"status": "ready"}

        with patch("sagasu.amendments.execute_amendment", side_effect=execute):
            await backend.run("validate_amend", payload, AsyncMock(), AsyncMock())
        with self.assertRaises(AmendmentUnavailable):
            await check_added_intervals(
                payload, AsyncMock(side_effect=LayoutChanged("PRIVATE FAILURE"))
            )
