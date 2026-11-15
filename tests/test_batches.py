"""Batch approval, replay, queueing, shutdown and individual durable outcomes."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from mcp_types import ElicitResult
from sagasu.action_journal import ActionJournal
from sagasu.fbs_client import BookingOutcomeUnknown
from sagasu.mcp_service import AgentError, AgentService
from sagasu.mcp_server import create_server
from tests.test_mcp import CONFIG, ROOM, ROW, TASK, ServiceCase, FakeBackend


class BatchTests(ServiceCase):
    async def reviews(self):
        reviews = []
        for date in ["01-Nov-2099", "02-Nov-2099"]:
            source = await self.finish(self.service.search({**CONFIG, "date": date}))
            reviews.append(
                self.service.review_booking(
                    source.id, ROOM["name"], "PRIVATE PURPOSE", "friend@smu.edu.sg"
                )
            )
        return reviews

    async def batch(self):
        reviews = await self.reviews()
        return reviews, self.service.review_batch(
            [review["review_id"] for review in reviews]
        )

    async def test_one_submission_uses_existing_write_jobs_and_replay_never_enqueues_again(
        self,
    ):
        reviews, batch = await self.batch()
        self.assertFalse(any(kind == "book" for kind, _ in self.backend.calls))
        first = self.service.submit(batch["review_id"])
        replay = self.service.submit(batch["review_id"])
        self.assertEqual(first["operation_id"], replay["operation_id"])
        job = await asyncio.wait_for(self.finish(first), 2)
        result = self.service.result(job.id, limit=1)["result"]
        self.assertTrue(result["complete"])
        self.assertTrue(result["all_operations_succeeded"])
        self.assertEqual(result["pagination"]["next_offset"], 1)
        self.assertEqual(
            result["members"][0]["result"]["fbs_status"], "Pending Confirmation"
        )
        self.assertNotIn("PRIVATE PURPOSE", json.dumps(result))
        self.assertEqual(
            len([kind for kind, _ in self.backend.calls if kind == "book"]), 2
        )
        for review, member in zip(reviews, job.result["members"]):
            self.assertEqual(
                self.service.submit(review["review_id"])["operation_id"],
                member["operation_id"],
            )
        self.assertEqual(job.batch_members, [])

    async def test_independent_unknown_and_successful_results_are_reported_without_retry(
        self,
    ):
        _, batch = await self.batch()
        original = self.backend.run

        async def run(kind, payload, progress, notify):
            if kind == "book" and payload.search.date == "01-Nov-2099":
                self.backend.calls.append((kind, payload))
                raise BookingOutcomeUnknown("PRIVATE FAILURE")
            return await original(kind, payload, progress, notify)

        self.backend.run = run
        job = await asyncio.wait_for(
            self.finish(self.service.submit(batch["review_id"])), 2
        )
        self.assertEqual(
            [member["status"] for member in job.result["members"]],
            ["outcome_unknown", "succeeded"],
        )
        self.assertFalse(job.result["all_operations_succeeded"])
        self.assertFalse(job.result["outcomes_known"])
        self.assertFalse(job.result["automatic_retries"])
        self.assertNotIn("PRIVATE FAILURE", json.dumps(job.result))
        self.assertEqual(
            len([kind for kind, _ in self.backend.calls if kind == "book"]), 2
        )

    async def test_duplicate_targets_nested_batches_and_competing_references_are_rejected(
        self,
    ):
        reviews, batch = await self.batch()
        source = await self.finish(self.service.search(CONFIG))
        duplicate = self.service.review_booking(
            source.id, ROOM["name"], "Other", "other@smu.edu.sg"
        )
        for review_ids in [
            [],
            [reviews[0]["review_id"]] * 2,
            [reviews[0]["review_id"], duplicate["review_id"]],
            [batch["review_id"]],
        ]:
            with self.subTest(review_ids=review_ids), self.assertRaises(AgentError):
                self.service.review_batch(review_ids)
        self.backend.tasks = [copy.deepcopy(TASK)]
        listing = await self.finish(self.service.start("bookings"))
        tasks = await self.finish(self.service.start("tasks"))
        cancel = self.service.review_cancellation(
            listing.id, ROW["reference_number"], "Plans changed"
        )
        approve = self.service.review_task_approval(tasks.id, TASK["reference_number"])
        with self.assertRaises(AgentError):
            self.service.review_batch([cancel["review_id"], approve["review_id"]])

    async def test_batch_expires_with_earliest_member_and_discarded_members_revoke_submission(
        self,
    ):
        reviews, batch = await self.batch()
        self.service.discard(reviews[1]["review_id"])
        with self.assertRaises(AgentError):
            self.service.submit(batch["review_id"])
        self.assertFalse(any(kind == "book" for kind, _ in self.backend.calls))
        self.now = batch["expires_at"]
        with self.assertRaises(AgentError):
            self.service.submit(batch["review_id"])

    async def test_running_batch_cannot_be_cancelled_but_a_queued_member_can(self):
        _, batch = await self.batch()
        self.backend.gate = asyncio.Event()
        started = asyncio.Event()
        original = self.backend.run

        async def run(*args):
            started.set()
            return await original(*args)

        self.backend.run = run
        operation = self.service.submit(batch["review_id"])
        await started.wait()
        group = self.service.get(operation["operation_id"])
        members = self.service.result(group.id)["result"]["members"]
        with self.assertRaises(AgentError):
            await self.service.cancel(group.id)
        await self.service.cancel(members[1]["operation_id"])
        self.backend.gate.set()
        await asyncio.wait_for(group.task, 2)
        self.assertEqual(
            [member["status"] for member in group.result["members"]],
            ["succeeded", "cancelled"],
        )
        self.assertEqual(
            len([kind for kind, _ in self.backend.calls if kind == "book"]), 1
        )

    async def test_authentication_is_reported_by_child_id_without_copying_private_challenge(
        self,
    ):
        _, batch = await self.batch()
        self.backend.mfa, self.backend.gate = True, asyncio.Event()
        operation = self.service.submit(batch["review_id"])
        group = self.service.get(operation["operation_id"])
        await asyncio.sleep(0)
        child = group.batch_members[0]["job"]
        await child.auth_event.wait()
        status = self.service.result(group.id)["result"]
        self.assertEqual(status["authentication_operations"], [child.id])
        self.assertNotIn("Approve this sign-in using 88", json.dumps(status))
        self.assertIn("88", self.service.authentication_status(child.id)["prompt"])
        self.backend.mfa = False
        self.backend.gate.set()
        await asyncio.wait_for(group.task, 2)

    async def test_queue_failure_is_per_item_and_consumed_review_cannot_be_replayed(
        self,
    ):
        reviews, batch = await self.batch()
        original = self.service.start

        def start(kind, payload=None, **kwargs):
            if kind == "book" and payload.search.date == "02-Nov-2099":
                raise AgentError("PRIVATE QUEUE CONTENT")
            return original(kind, payload, **kwargs)

        self.service.start = start
        group = await self.finish(self.service.submit(batch["review_id"]))
        self.assertEqual(
            [member["status"] for member in group.result["members"]],
            ["succeeded", "queue_failed"],
        )
        self.assertFalse(group.result["outcomes_known"])
        self.assertNotIn("PRIVATE QUEUE CONTENT", json.dumps(group.result))
        self.assertEqual(
            self.service.submit(reviews[1]["review_id"])["operation_id"], group.id
        )

    async def test_shutdown_does_not_interrupt_a_running_native_write(self):
        _, batch = await self.batch()
        self.backend.gate = asyncio.Event()
        started = asyncio.Event()
        original = self.backend.run

        async def run(*args):
            started.set()
            return await original(*args)

        self.backend.run = run
        queued = self.service.submit(batch["review_id"])
        await started.wait()
        group = self.service.get(queued["operation_id"])
        first = group.batch_members[0]["job"]
        closer = asyncio.create_task(self.service.close())
        await asyncio.sleep(0)
        self.assertFalse(closer.done())
        self.assertFalse(first.task.cancelled())
        self.backend.gate.set()
        await asyncio.wait_for(closer, 2)
        self.assertEqual(first.status, "succeeded")
        self.assertTrue(group.terminal)

    async def test_individual_history_survives_restart_without_replaying_batch_monitor(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            journal = ActionJournal(Path(temporary) / "actions.sqlite3")
            self.service.journal = journal
            _, batch = await self.batch()
            group = await self.finish(self.service.submit(batch["review_id"]))
            child_ids = [member["operation_id"] for member in group.result["members"]]
            await self.service.close()
            restarted = AgentService(backend=FakeBackend(), journal=journal)
            self.addAsyncCleanup(restarted.close)
            for operation_id in child_ids:
                self.assertEqual(restarted.get(operation_id).status, "succeeded")
            with self.assertRaises(AgentError):
                restarted.submit(batch["review_id"])
            self.assertEqual(restarted.backend.calls, [])
            self.assertNotIn("PRIVATE PURPOSE", json.dumps(journal.history()))
            self.assertNotIn("friend@smu.edu.sg", json.dumps(journal.history()))

    async def test_human_policy_decline_blocks_and_one_acceptance_covers_exact_batch(
        self,
    ):
        reviews = await self.reviews()
        asked = []

        async def decline(context, params):
            asked.append(params.message)
            return ElicitResult(action="decline")

        async with Client(
            create_server(self.service), elicitation_callback=decline
        ) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            self.assertNotIn("submit_review", tools)
            batch = await client.call_tool(
                "batch_review", {"review_ids": [r["review_id"] for r in reviews]}
            )
            self.assertFalse(batch.is_error)
            reply = await client.call_tool(
                "confirm_review", {"review_id": batch.structured_content["review_id"]}
            )
            self.assertFalse(reply.structured_content["submitted"])
        self.assertEqual(len(asked), 1)
        self.assertIn("PRIVATE PURPOSE", asked[0])
        self.assertFalse(any(kind == "book" for kind, _ in self.backend.calls))

        async def accept(context, params):
            asked.append(params.message)
            return ElicitResult(action="accept", content={"confirm": True})

        async with Client(
            create_server(self.service), elicitation_callback=accept
        ) as client:
            reply = await client.call_tool(
                "confirm_review", {"review_id": batch.structured_content["review_id"]}
            )
            self.assertFalse(reply.is_error)
            group = await self.finish(reply.structured_content)
        self.assertEqual(len(asked), 2)
        self.assertIn("01-Nov-2099", asked[1])
        self.assertIn("02-Nov-2099", asked[1])
        self.assertTrue(group.result["all_operations_succeeded"])

    async def test_mixed_native_actions_keep_their_statuses_and_confirmation_roles(
        self,
    ):
        reviews = await self.reviews()
        self.backend.tasks = [
            {**copy.deepcopy(TASK), "reference_number": "BK-SEPARATE-TASK"}
        ]
        listing = await self.finish(self.service.start("bookings"))
        tasks = await self.finish(self.service.start("tasks"))
        cancel = self.service.review_cancellation(
            listing.id, ROW["reference_number"], "Plans changed"
        )
        approve = self.service.review_task_approval(tasks.id, "BK-SEPARATE-TASK")
        batch = self.service.review_batch(
            [reviews[1]["review_id"], cancel["review_id"], approve["review_id"]]
        )
        group = await self.finish(self.service.submit(batch["review_id"]))
        self.assertEqual(
            [member["kind"] for member in group.result["members"]],
            ["book", "cancel", "approve_task"],
        )
        results = [member["result"] for member in group.result["members"]]
        self.assertEqual(results[0]["fbs_status"], "Pending Confirmation")
        self.assertEqual(results[1]["fbs_status"], "Cancelled")
        self.assertEqual(results[2]["task_status"], "Approved")
        self.assertFalse(results[2]["booking_status_verified"])

    async def test_queued_batch_cancellation_and_history_failure_never_launch_children(
        self,
    ):
        reviews, batch = await self.batch()
        self.service.journal_failed = True
        with self.assertRaises(AgentError):
            self.service.submit(batch["review_id"])
        self.assertIn(reviews[0]["review_id"], self.service.reviews)
        self.service.journal_failed = False
        queued = self.service.submit(batch["review_id"])
        cancelled = await self.service.cancel(queued["operation_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            self.service.submit(batch["review_id"])["operation_id"],
            queued["operation_id"],
        )
        self.assertFalse(
            any(
                kind in {"book", "cancel", "approve_task"}
                for kind, _ in self.backend.calls
            )
        )
