"""Durability, crash boundaries, privacy and replay protection; never live FBS."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sagasu.action_journal import ActionJournal, JournalError, checkpoint_verified
from sagasu.mcp_service import AgentService, AgentError, Job
from tests.test_mcp import CONFIG, ROOM, ROW, TASK, FakeBackend, ServiceCase


class JournalServiceTests(ServiceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private/actions.sqlite3"
        self.service.journal = ActionJournal(self.path)

    async def test_each_write_survives_restart_and_same_review_never_replays(self):
        self.backend.tasks = [TASK]
        reviews = [await self.review()]
        source = await self.finish(self.service.start("bookings"))
        reviews.append(self.service.review_cancellation(source.id, ROW["reference_number"], "private reason"))
        source = await self.finish(self.service.start("tasks"))
        reviews.append(self.service.review_task_approval(source.id, TASK["reference_number"]))
        operations = [await self.finish(self.service.submit(review["review_id"])) for review in reviews]
        await self.service.close()
        journal = ActionJournal(self.path)
        journal.recover(self.now)
        restarted = AgentService(backend=self.backend, journal=journal)
        self.addAsyncCleanup(restarted.close)
        for review, original in zip(reviews, operations):
            replay = restarted.submit(review["review_id"])
            self.assertEqual(replay["operation_id"], original.id)
            self.assertTrue(replay["recovered"])
            self.assertFalse(replay["receipt_available"])
            self.assertEqual(restarted.result(original.id)["result"]["status"], original.result["status"])
        self.assertEqual(sum(kind in {"book", "cancel", "approve_task"} for kind, _ in self.backend.calls), 3)
        self.assertEqual(restarted.action_history(limit=1)["next_offset"], 1)

    async def test_expired_memory_cache_still_deduplicates_durable_review(self):
        review = await self.review()
        original = await self.finish(self.service.submit(review["review_id"]))
        self.now += 3601
        self.service.prune()
        self.assertNotIn(original.id, self.service.jobs)
        self.assertEqual(self.service.submit(review["review_id"])["operation_id"], original.id)
        self.assertEqual([kind for kind, _ in self.backend.calls].count("book"), 1)

    async def test_failed_reservation_prevents_backend_and_keeps_review(self):
        review = await self.review()
        with patch.object(self.service.journal, "reserve", side_effect=JournalError("Storage unavailable")):
            with self.assertRaises(JournalError):
                self.service.submit(review["review_id"])
        self.assertIn(review["review_id"], self.service.reviews)
        self.assertNotIn("book", [kind for kind, _ in self.backend.calls])

    async def test_journal_failure_before_backend_blocks_writes_and_never_submits(self):
        review = await self.review()
        with patch.object(self.service.journal, "update", side_effect=JournalError("Storage unavailable")):
            job = await self.finish(self.service.submit(review["review_id"]))
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(job.history_warning)
        self.assertNotIn("book", [kind for kind, _ in self.backend.calls])
        next_review = await self.review()
        with self.assertRaises(AgentError):
            self.service.submit(next_review["review_id"])

    async def test_verified_outcome_survives_receipt_error_and_history_failure(self):
        review = await self.review()
        async def backend(kind, payload, progress, notify):
            checkpoint_verified({"status": "verified", "reference_number": ROW["reference_number"], "fbs_status": "Pending Confirmation"})
            raise RuntimeError("receipt failed with private details")
        self.backend.run = backend
        job = await self.finish(self.service.submit(review["review_id"]))
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result["reference_number"], ROW["reference_number"])
        self.assertIn("receipt_error", job.result)
        self.assertEqual(self.service.journal.get(job.id)["status"], "succeeded")

    async def test_history_omits_payloads_auth_and_screenshots(self):
        source = await self.finish(self.service.search(CONFIG))
        review = self.service.review_booking(source.id, ROOM["name"], "PRIVATE_PURPOSE_SENTINEL", "private.person@smu.edu.sg")
        await self.finish(self.service.submit(review["review_id"]))
        stored = self.path.read_bytes()
        for sentinel in (b"PRIVATE_PURPOSE_SENTINEL", b"private.person@smu.edu.sg", b"receipt_png", b"password", b"Approve this sign-in"):
            self.assertNotIn(sentinel, stored)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class JournalCrashTests(unittest.TestCase):
    def test_process_crashes_before_and_after_verification_never_replay(self):
        script = '''
import asyncio, json, os, sys
from pathlib import Path
from sagasu.action_journal import ActionJournal, checkpoint_verified
from sagasu.mcp_service import AgentService
from tests.test_mcp import FakeBackend, CONFIG, ROOM
root, phase = Path(sys.argv[1]), sys.argv[2]
class Backend(FakeBackend):
    async def run(self, kind, payload, progress, notify):
        if kind == "book":
            (root / "submitted").write_text("one fake submission")
            if phase == "verified":
                checkpoint_verified({"status":"verified", "reference_number":"BK-TEST-123", "fbs_status":"Confirmed"})
            os._exit(77)
        return await super().run(kind, payload, progress, notify)
async def main():
    service = AgentService(backend=Backend(), journal=ActionJournal(root / "actions.sqlite3"))
    source = service.search(CONFIG)
    await service.get(source["operation_id"]).task
    review = service.review_booking(source["operation_id"], ROOM["name"], "PRIVATE_PURPOSE", "private.person@smu.edu.sg")
    operation = service.submit(review["review_id"])
    (root / "ids.json").write_text(json.dumps({"review_id":review["review_id"], "operation_id":operation["operation_id"]}))
    await service.get(operation["operation_id"]).task
asyncio.run(main())
'''
        for phase, expected in (("submitted", "outcome_unknown"), ("verified", "succeeded")):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = subprocess.run([sys.executable, "-c", script, directory, phase], capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 77, result.stderr.decode())
                ids = json.loads((root / "ids.json").read_text())
                journal = ActionJournal(root / "actions.sqlite3")
                journal.recover(time.time())
                record = journal.get(ids["operation_id"])
                self.assertEqual(record["status"], expected)
                self.assertEqual(record["reconciliation_required"], expected == "outcome_unknown")
                backend = FakeBackend()
                restarted = AgentService(backend=backend, journal=journal)
                self.assertEqual(restarted.submit(ids["review_id"])["operation_id"], ids["operation_id"])
                self.assertEqual(backend.calls, [])
                self.assertEqual((root / "submitted").read_text(), "one fake submission")
                if phase == "verified":
                    self.assertEqual(record["result"]["reference_number"], "BK-TEST-123")

    def test_queued_recovery_is_cancelled_and_unknown_schema_or_corruption_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.sqlite3"
            journal = ActionJournal(path)
            journal.reserve(Job("op", "book", 1), "review", {"room": "A"})
            journal.recover(2)
            self.assertEqual(journal.get("op")["status"], "cancelled")
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=99")
            db.close()
            with self.assertRaises(JournalError):
                journal.recover(3)
            path.write_bytes(b"private corrupt data")
            with self.assertRaises(JournalError):
                journal.history()
            self.assertEqual(path.read_bytes(), b"private corrupt data")
