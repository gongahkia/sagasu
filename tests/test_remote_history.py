"""A remote caller never sees what the person at the host did: in memory, in
action_history, and in results recovered from SQLite after a restart or after
the in-memory job expired. Only local fixtures; nothing reaches FBS."""

import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

from sagasu.action_journal import ActionJournal, JournalError
from sagasu.fbs_client import BookingOutcomeUnknown
from sagasu.mcp_service import AgentError, AgentService
from tests.test_mcp import CONFIG, ROOM, FakeBackend
from tests.test_remote import remote_origin


class RemoteHistoryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "actions.sqlite3"
        self.now = 1000.0
        self.backend = FakeBackend()
        self.service = self.make()

    def make(self):
        service = AgentService(
            backend=self.backend,
            journal=ActionJournal(self.path),
            clock=lambda: self.now,
        )
        self.addAsyncCleanup(service.close)
        return service

    async def finish(self, service, job):
        await service.get(job["operation_id"]).task
        return service.get(job["operation_id"])

    async def book(self, *, remote, room=ROOM["name"], unknown=False):
        """One booking write, started by the host's person or a remote caller."""
        with remote_origin() if remote else nullcontext():
            search = await self.finish(self.service, self.service.search(CONFIG))
            review = self.service.review_booking(
                search.id, room, "Project meeting", "friend@scis.smu.edu.sg"
            )
            self.backend.error = BookingOutcomeUnknown("private") if unknown else None
            try:
                job = await self.finish(
                    self.service, self.service.submit(review["review_id"])
                )
            finally:
                self.backend.error = None
        return job.id

    async def three_writes(self):
        # The uncertain write last: it would (rightly) block a repeat of its slot.
        remote = await self.book(remote=True)
        local = await self.book(remote=False)
        local_unknown = await self.book(remote=False, unknown=True)
        return local, local_unknown, remote

    def assert_remote_cannot_reach(self, service, *operation_ids):
        with remote_origin():
            history = service.action_history(0, 50)
            listed = {action["operation_id"] for action in history["actions"]}
            for operation_id in operation_ids:
                with self.subTest(operation_id=operation_id):
                    self.assertNotIn(operation_id, listed)
                    for call in (service.get, service.reconcile_operation):
                        with self.assertRaises(AgentError) as caught:
                            call(operation_id)
                        self.assertIn(
                            "Unknown or expired operation", str(caught.exception)
                        )
        return history

    async def test_after_restart_remote_history_and_recovered_results_hide_local_writes(
        self,
    ):
        local, local_unknown, remote = await self.three_writes()
        restarted = self.make()  # same SQLite file, nothing in memory
        history = self.assert_remote_cannot_reach(restarted, local, local_unknown)
        self.assertEqual([a["operation_id"] for a in history["actions"]], [remote])
        self.assertEqual(history["total"], 1)
        self.assertIsNone(history["next_offset"])
        with remote_origin():
            recovered = restarted.get(remote)
            self.assertTrue(recovered.recovered)
            self.assertTrue(recovered.remote)
        # The person at the host still sees and can reconcile everything.
        everything = restarted.action_history(0, 50)
        self.assertEqual(everything["total"], 3)
        self.assertEqual(
            {a["operation_id"]: a["origin"] for a in everything["actions"]},
            {local: "local", local_unknown: "local", remote: "remote"},
        )
        self.assertEqual(restarted.get(local_unknown).status, "outcome_unknown")
        restarted.reconcile_operation(local_unknown)

    async def test_after_expiry_remote_history_and_recovered_results_hide_local_writes(
        self,
    ):
        local, local_unknown, remote = await self.three_writes()
        self.now += self.service.ttl + 1
        self.service.prune()
        for operation_id in (local, local_unknown, remote):
            self.assertNotIn(operation_id, self.service.jobs)
        history = self.assert_remote_cannot_reach(self.service, local, local_unknown)
        self.assertEqual([a["operation_id"] for a in history["actions"]], [remote])
        with remote_origin():
            self.assertTrue(self.service.get(remote).remote)
        self.assertEqual(self.service.action_history(0, 50)["total"], 3)

    async def test_paging_counts_only_what_a_remote_caller_may_see(self):
        await self.book(remote=False)
        ids = [await self.book(remote=True) for _ in range(3)]
        with remote_origin():
            first = self.make().action_history(0, 2)
            self.assertEqual(first["total"], 3)
            self.assertEqual(first["next_offset"], 2)
            second = self.make().action_history(2, 2)
        listed = [a["operation_id"] for a in first["actions"] + second["actions"]]
        self.assertEqual(sorted(listed), sorted(ids))

    async def test_actions_recorded_before_origins_were_kept_count_as_local(self):
        remote = await self.book(remote=True)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM action_origins")  # as an older version left it
        restarted = self.make()
        self.assert_remote_cannot_reach(restarted, remote)
        self.assertEqual(restarted.action_history(0, 50)["total"], 1)

    async def test_a_hidden_local_action_blocks_a_remote_repeat_without_being_named(
        self,
    ):
        local_unknown = await self.book(remote=False, unknown=True)
        for service in (self.service, self.make()):  # in memory, then after restart
            with self.subTest(restarted=service is not self.service), remote_origin():
                search = await self.finish(service, service.search(CONFIG))
                review = service.review_booking(
                    search.id, ROOM["name"], "Project meeting", "friend@scis.smu.edu.sg"
                )
                # In memory the service refuses; after a restart the journal does.
                with self.assertRaises((AgentError, JournalError)) as caught:
                    service.submit(review["review_id"])
                self.assertIn("started at the host", str(caught.exception))
                self.assertNotIn(local_unknown, str(caught.exception))

    async def test_remote_caller_still_reconciles_its_own_recovered_write(self):
        remote_unknown = await self.book(remote=True, unknown=True)
        restarted = self.make()
        with remote_origin():
            reconcile = restarted.reconcile_operation(remote_unknown)
            self.assertEqual(reconcile["kind"], "reconcile")


class WatchOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_host_gets_its_own_watch_even_after_a_remote_one(self):
        # A remote caller could cancel its own watcher; the host's watch must
        # not ride on it.
        backend = FakeBackend()
        backend.gate = __import__("asyncio").Event()  # keep both watches running
        service = AgentService(backend=backend)
        self.addAsyncCleanup(service.close)
        with remote_origin():
            theirs = service.watch_booking("BK-TEST-1")
        mine = service.watch_booking("BK-TEST-1")
        self.assertNotEqual(theirs["operation_id"], mine["operation_id"])
        self.assertEqual(
            service.watch_booking("BK-TEST-1")["operation_id"], mine["operation_id"]
        )
        with remote_origin():
            self.assertEqual(
                service.watch_booking("BK-TEST-1")["operation_id"],
                theirs["operation_id"],
            )
        backend.gate.set()
