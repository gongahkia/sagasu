"""A remote caller sees, and can address, only what remote calls created, through
the MCP tools as well as the service: after a restart, after expiry, after
eviction at the job limit and after a crash. Reconcile and booking watches keep
the same rule; the person at the host still sees everything.

Only local fixtures: a fake backend, temporary SQLite files and in-process MCP
clients. Nothing reaches FBS or Microsoft."""

import asyncio
import copy
import json
import secrets
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

from sagasu.action_journal import ActionJournal, JournalError
from sagasu.fbs_client import BookingOutcomeUnknown
from sagasu.mcp_service import AgentError, AgentService
from sagasu.reconciliation import evidence
from tests.test_mcp import CONFIG, ROOM, ROW, FakeBackend
from tests.test_remote import remote_origin

try:
    from mcp import Client
    from sagasu.mcp_server import RemoteAccess, create_server
except ImportError:
    Client = None


ROOMS = [ROOM["name"], "SCIS1 GSR 2-2", "SCIS1 GSR 2-3"]
CO_BOOKER = "friend@scis.smu.edu.sg"
SETUP = {"mode": "operational", "operational": True, "checks": [], "authentication": {}}
UNKNOWN = "Unknown or expired operation"
CAP = "At most four booking watches may run at once on this server."
# Every tool that addresses one operation by its ID, with its other arguments.
BY_ID = (
    ("operation_result", {}),
    ("operation_status", {}),
    ("wait_operation", {"timeout": 0}),
    ("support_bundle", {}),
    ("operation_receipt", {"failure": True}),
    ("operation_receipt", {"failure": False}),
    ("booking_receipt", {}),
    ("reconcile_operation", {}),
    ("cancel_operation", {}),
)


class RoomsBackend(FakeBackend):
    """Three free rooms (so writes need not collide), and a reconcile that
    returns evidence shaped like sagasu.reconciliation.evidence()."""

    def __init__(self):
        super().__init__()
        self.evidence = evidence("unresolved", complete=True)

    async def run(self, kind, payload, progress, notify):
        if kind == "reconcile":
            self.calls.append((kind, payload))
            await progress("Reading My Bookings")
            return copy.deepcopy(self.evidence)
        result = await super().run(kind, payload, progress, notify)
        if kind == "search":
            result["rooms"] = [{**copy.deepcopy(ROOM), "name": name} for name in ROOMS]
        return result


@unittest.skipIf(Client is None, "Install Sagasu for MCP runtime tests")
class ToolCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "actions.sqlite3"
        self.now = 1000.0
        self.backend = RoomsBackend()
        self.service = self.make()

    def make(self, backend=None, **options):
        """A service on the shared SQLite journal: a restart when one exists."""
        service = AgentService(
            backend=backend or self.backend,
            journal=ActionJournal(self.path),
            clock=lambda: self.now,
            **options,
        )
        self.addAsyncCleanup(service.close)
        return service

    def client(self, service, *, remote):
        return Client(
            create_server(
                service,
                allow_agent_submission=True,
                configuration_provider=lambda: SETUP,
                remote=RemoteAccess(writes=True) if remote else None,
            )
        )

    async def call(self, client, name, arguments):
        result = await client.call_tool(name, arguments)
        text = "\n".join(getattr(part, "text", None) or "" for part in result.content)
        return result.is_error, text, result.structured_content

    async def ok(self, client, name, arguments):
        failed, text, content = await self.call(client, name, arguments)
        self.assertFalse(failed, f"{name} failed: {text}")
        return content

    async def review(self, client, room):
        search = await self.ok(client, "search_rooms", {"config": CONFIG})
        await self.ok(
            client,
            "wait_operation",
            {"operation_id": search["operation_id"], "timeout": 5},
        )
        return await self.ok(
            client,
            "booking_review",
            {
                "search_operation_id": search["operation_id"],
                "room": room,
                "purpose": "Project meeting",
                "co_booker": CO_BOOKER,
            },
        )

    async def book(self, service, room, *, remote, unknown=False):
        """One booking write made through the tools by a remote caller or the host."""
        async with self.client(service, remote=remote) as client:
            review = await self.review(client, room)
            service.backend.error = (
                BookingOutcomeUnknown("private") if unknown else None
            )
            try:
                job = await self.ok(
                    client, "submit_review", {"review_id": review["review_id"]}
                )
                done = await self.ok(
                    client,
                    "wait_operation",
                    {"operation_id": job["operation_id"], "timeout": 5},
                )
            finally:
                service.backend.error = None
        self.assertEqual(done["status"], "outcome_unknown" if unknown else "succeeded")
        return job["operation_id"]

    async def assert_like_unknown(self, client, operation_id, tools=BY_ID):
        """Every tool answers this ID exactly as it answers an ID never issued."""
        for name, extra in tools:
            with self.subTest(operation_id=operation_id, tool=name, **extra):
                hidden = await self.call(
                    client, name, {"operation_id": operation_id, **extra}
                )
                never = await self.call(
                    client,
                    name,
                    {"operation_id": secrets.token_urlsafe(16), **extra},
                )
                self.assertTrue(hidden[0])
                self.assertIn(UNKNOWN, hidden[1])
                self.assertEqual(hidden[:2], never[:2])
                self.assertNotIn(operation_id, hidden[1])

    async def until(self, condition, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not condition():
            if loop.time() > deadline:
                self.fail("Condition not reached in time.")
            await asyncio.sleep(0.01)


class RecoveredWritesThroughToolsTests(ToolCase):
    async def four_writes(self):
        remote = await self.book(self.service, ROOMS[0], remote=True)
        local = await self.book(self.service, ROOMS[0], remote=False)
        local_unknown = await self.book(
            self.service, ROOMS[1], remote=False, unknown=True
        )
        remote_unknown = await self.book(
            self.service, ROOMS[2], remote=True, unknown=True
        )
        return [local, local_unknown], [remote, remote_unknown]

    async def assert_isolated(self, service, local_ids, remote_ids):
        async with self.client(service, remote=True) as client:
            history = await self.ok(client, "action_history", {"limit": 50})
            self.assertEqual(
                {action["operation_id"] for action in history["actions"]},
                set(remote_ids),
            )
            self.assertEqual(history["total"], len(remote_ids))
            self.assertIsNone(history["next_offset"])
            for operation_id in local_ids:
                await self.assert_like_unknown(client, operation_id)
            # Its own recovered writes still answer through every tool.
            for operation_id in remote_ids:
                with self.subTest(own=operation_id):
                    result = await self.ok(
                        client, "operation_result", {"operation_id": operation_id}
                    )
                    self.assertEqual(result["operation_id"], operation_id)
                    self.assertTrue(result["recovered"])
                    status = await self.ok(
                        client, "operation_status", {"operation_id": operation_id}
                    )
                    self.assertEqual(status["status"], result["status"])
                    waited = await self.ok(
                        client,
                        "wait_operation",
                        {"operation_id": operation_id, "timeout": 0},
                    )
                    self.assertTrue(waited["terminal"])
                    bundle = await self.ok(
                        client, "support_bundle", {"operation_id": operation_id}
                    )
                    self.assertTrue(bundle["operation"]["recovered"])
                    # Found, but images die with the process: a different answer.
                    failed, text, _ = await self.call(
                        client,
                        "operation_receipt",
                        {"operation_id": operation_id, "failure": True},
                    )
                    self.assertTrue(failed)
                    self.assertIn("No automatic failure screenshot", text)
                    for name, extra in (
                        ("operation_receipt", {"failure": False}),
                        ("booking_receipt", {}),
                    ):
                        failed, text, _ = await self.call(
                            client, name, {"operation_id": operation_id, **extra}
                        )
                        self.assertTrue(failed)
                        self.assertIn("No receipt is available", text)
                    stopped = await self.ok(
                        client, "cancel_operation", {"operation_id": operation_id}
                    )
                    self.assertEqual(stopped["status"], result["status"])
                    started = await self.ok(
                        client, "reconcile_operation", {"operation_id": operation_id}
                    )
                    self.assertEqual(started["kind"], "reconcile")
                    done = await self.ok(
                        client,
                        "wait_operation",
                        {"operation_id": started["operation_id"], "timeout": 5},
                    )
                    self.assertEqual(done["status"], "succeeded")
        reconciled = [
            p["operation_id"] for k, p in self.backend.calls if k == "reconcile"
        ]
        self.assertEqual(sorted(reconciled), sorted(remote_ids))
        for operation_id in local_ids:
            self.assertIsNone(service.journal.get(operation_id)["reconciliation"])
        # The person at the host lists and reads both kinds.
        async with self.client(service, remote=False) as client:
            history = await self.ok(client, "action_history", {"limit": 50})
            self.assertEqual(
                {a["operation_id"]: a["origin"] for a in history["actions"]},
                {
                    **{operation_id: "local" for operation_id in local_ids},
                    **{operation_id: "remote" for operation_id in remote_ids},
                },
            )
            self.assertEqual(history["total"], len(local_ids) + len(remote_ids))
            for operation_id in [*local_ids, *remote_ids]:
                result = await self.ok(
                    client, "operation_result", {"operation_id": operation_id}
                )
                self.assertTrue(result["recovered"])

    async def test_after_a_restart_tools_hide_local_writes_from_remote_callers(self):
        local_ids, remote_ids = await self.four_writes()
        await self.service.close()
        restarted = self.make()  # same SQLite journal, nothing in memory
        await self.assert_isolated(restarted, local_ids, remote_ids)
        self.assertEqual([k for k, _ in self.backend.calls].count("book"), 4)

    async def test_after_expiry_tools_hide_local_writes_from_remote_callers(self):
        local_ids, remote_ids = await self.four_writes()
        self.now += self.service.ttl + 1
        self.service.prune()
        for operation_id in [*local_ids, *remote_ids]:
            self.assertNotIn(operation_id, self.service.jobs)
        await self.assert_isolated(self.service, local_ids, remote_ids)
        self.assertEqual([k for k, _ in self.backend.calls].count("book"), 4)


class ReconcileCompletesTests(ToolCase):
    async def test_remote_caller_reconciles_its_own_recovered_write_into_the_journal(
        self,
    ):
        remote_unknown = await self.book(
            self.service, ROOMS[0], remote=True, unknown=True
        )
        restarted = self.make()
        async with self.client(restarted, remote=True) as client:
            started = await self.ok(
                client, "reconcile_operation", {"operation_id": remote_unknown}
            )
            self.assertEqual(started["kind"], "reconcile")
            done = await self.ok(
                client,
                "wait_operation",
                {"operation_id": started["operation_id"], "timeout": 5},
            )
            self.assertEqual(done["status"], "succeeded")
            self.assertTrue(done["terminal"])
            self.assertEqual(done["result"]["state"], "unresolved")
            self.assertEqual(done["result"]["original_operation_id"], remote_unknown)
            recorded = restarted.journal.get(remote_unknown)["reconciliation"]
            self.assertEqual(recorded["state"], "unresolved")
            self.assertFalse(recorded["action_outcome_verified"])
            self.assertEqual(recorded["observed_at"], done["result"]["observed_at"])
            original = await self.ok(
                client, "operation_result", {"operation_id": remote_unknown}
            )
            self.assertEqual(original["status"], "outcome_unknown")
            self.assertEqual(original["reconciliation"]["state"], "unresolved")
            self.assertTrue(original["reconciliation_required"])

            # Verified evidence settles it, and a remote caller cannot replace it.
            self.backend.evidence = evidence(
                "pending_confirmation",
                verified=True,
                reference=ROW["reference_number"],
                status="Pending Confirmation",
                complete=True,
            )
            started = await self.ok(
                client, "reconcile_operation", {"operation_id": remote_unknown}
            )
            done = await self.ok(
                client,
                "wait_operation",
                {"operation_id": started["operation_id"], "timeout": 5},
            )
            self.assertEqual(done["status"], "succeeded")
            recorded = restarted.journal.get(remote_unknown)["reconciliation"]
            self.assertTrue(recorded["action_outcome_verified"])
            self.assertEqual(recorded["reference_number"], ROW["reference_number"])
            original = await self.ok(
                client, "operation_result", {"operation_id": remote_unknown}
            )
            self.assertFalse(original["reconciliation_required"])
            failed, text, _ = await self.call(
                client, "reconcile_operation", {"operation_id": remote_unknown}
            )
            self.assertTrue(failed)
            self.assertIn("already verified", text)
        reconciled = [
            p["operation_id"] for k, p in self.backend.calls if k == "reconcile"
        ]
        self.assertEqual(reconciled, [remote_unknown, remote_unknown])
        self.assertEqual([k for k, _ in self.backend.calls].count("book"), 1)

    async def test_host_reconciles_a_local_write_after_expiry_unseen_by_remote(self):
        local_unknown = await self.book(
            self.service, ROOMS[0], remote=False, unknown=True
        )
        self.now += self.service.ttl + 1
        self.service.prune()
        self.assertNotIn(local_unknown, self.service.jobs)
        self.backend.evidence = evidence(
            "matching_booking_found",
            reference=ROW["reference_number"],
            status="Pending Confirmation",
            complete=True,
        )
        async with self.client(self.service, remote=False) as client:
            started = await self.ok(
                client, "reconcile_operation", {"operation_id": local_unknown}
            )
            done = await self.ok(
                client,
                "wait_operation",
                {"operation_id": started["operation_id"], "timeout": 5},
            )
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"]["state"], "matching_booking_found")
        self.assertEqual(done["result"]["original_operation_id"], local_unknown)
        recorded = self.service.journal.get(local_unknown)["reconciliation"]
        self.assertEqual(recorded["state"], "matching_booking_found")
        self.assertFalse(recorded["action_outcome_verified"])
        self.assertEqual(recorded["reference_number"], ROW["reference_number"])
        # Neither the write nor the host's reconcile job reaches a remote caller.
        async with self.client(self.service, remote=True) as client:
            for operation_id in (local_unknown, started["operation_id"]):
                await self.assert_like_unknown(client, operation_id)
            history = await self.ok(client, "action_history", {})
            self.assertEqual((history["actions"], history["total"]), ([], 0))
        reconciled = [
            p["operation_id"] for k, p in self.backend.calls if k == "reconcile"
        ]
        self.assertEqual(reconciled, [local_unknown])


class DuplicateBlockTests(ToolCase):
    async def test_remote_caller_blocked_by_its_own_unresolved_write_is_told_which(
        self,
    ):
        remote_unknown = await self.book(
            self.service, ROOMS[0], remote=True, unknown=True
        )
        # Service level: AgentError in memory; JournalError from SQLite after a restart.
        for service, error in ((self.service, AgentError), (self.make(), JournalError)):
            with self.subTest(restarted=service is not self.service), remote_origin():
                search = service.search(CONFIG)
                await service.get(search["operation_id"]).task
                review = service.review_booking(
                    search["operation_id"], ROOMS[0], "Again", CO_BOOKER
                )
                with self.assertRaises(error) as caught:
                    service.submit(review["review_id"])
                self.assertIn(remote_unknown, str(caught.exception))
                self.assertNotIn("started at the host", str(caught.exception))
        # Tool level, in memory and after a restart, for the remote caller and
        # for the person at the host (who sees everything).
        for service in (self.service, self.make()):
            for remote in (True, False):
                with self.subTest(restarted=service is not self.service, remote=remote):
                    async with self.client(service, remote=remote) as client:
                        review = await self.review(client, ROOMS[0])
                        failed, text, _ = await self.call(
                            client, "submit_review", {"review_id": review["review_id"]}
                        )
                    self.assertTrue(failed)
                    self.assertIn(remote_unknown, text)
                    self.assertNotIn("started at the host", text)
        self.assertEqual([k for k, _ in self.backend.calls].count("book"), 1)


class CrashRecoveryTests(ToolCase):
    async def running_write(self, service, room, *, remote):
        """Start a write and leave it running, as when the process dies."""
        with remote_origin() if remote else nullcontext():
            search = service.search(CONFIG)
            await service.get(search["operation_id"]).task
            review = service.review_booking(
                search["operation_id"], room, "Project meeting", CO_BOOKER
            )
            service.backend.gate = asyncio.Event()
            # Cleanups run last-in first-out: the gate opens before close().
            self.addCleanup(service.backend.gate.set)
            job = service.submit(review["review_id"])
        operation_id = job["operation_id"]
        await self.until(
            lambda: service.journal.get(operation_id)["status"] == "running"
        )
        self.assertEqual(service.get(operation_id).remote, remote)
        return operation_id

    def restart(self):
        service = self.make(RoomsBackend())
        service.journal.recover(self.now)  # as mcp_runtime does at startup
        return service

    async def test_a_write_running_when_the_process_died_keeps_its_origin(self):
        first = self.make(RoomsBackend())
        remote = await self.running_write(first, ROOMS[0], remote=True)
        second = self.restart()  # the first process died mid-write
        with remote_origin():
            recovered = second.get(remote)
            self.assertTrue(recovered.recovered)
            self.assertTrue(recovered.remote)
            self.assertEqual(recovered.status, "outcome_unknown")
            self.assertEqual(recovered.error["code"], "ProcessInterrupted")
        local = await self.running_write(second, ROOMS[1], remote=False)
        third = self.restart()  # and so did the second
        self.assertEqual(third.journal.get(remote)["origin"], "remote")
        self.assertEqual(third.journal.get(local)["origin"], "local")
        async with self.client(third, remote=True) as client:
            history = await self.ok(client, "action_history", {})
            self.assertEqual(
                [action["operation_id"] for action in history["actions"]], [remote]
            )
            self.assertEqual(history["total"], 1)
            own = await self.ok(client, "operation_result", {"operation_id": remote})
            self.assertEqual(own["status"], "outcome_unknown")
            self.assertEqual(own["error"]["code"], "ProcessInterrupted")
            self.assertTrue(own["reconciliation_required"])
            await self.assert_like_unknown(client, local)
        async with self.client(third, remote=False) as client:
            history = await self.ok(client, "action_history", {})
            self.assertEqual(
                {
                    a["operation_id"]: (a["origin"], a["status"])
                    for a in history["actions"]
                },
                {
                    remote: ("remote", "outcome_unknown"),
                    local: ("local", "outcome_unknown"),
                },
            )
            mine = await self.ok(client, "operation_result", {"operation_id": local})
            self.assertEqual(mine["error"]["code"], "ProcessInterrupted")


class EvictionTests(ToolCase):
    async def test_a_local_write_evicted_at_the_job_limit_is_hidden_from_remote(self):
        service = self.make(limit=2)
        local = await self.book(service, ROOMS[0], remote=False, unknown=True)
        remote = await self.book(service, ROOMS[1], remote=True)
        # Evicted to make room for the remote caller's jobs, not expired.
        self.assertNotIn(local, service.jobs)
        self.assertIn(remote, service.jobs)
        async with self.client(service, remote=True) as client:
            await self.assert_like_unknown(client, local)
            history = await self.ok(client, "action_history", {})
            self.assertEqual([a["operation_id"] for a in history["actions"]], [remote])
            self.assertEqual(history["total"], 1)
            # Two more reads evict its own write too; it still reads it back.
            for _ in range(2):
                read = await self.ok(client, "list_bookings", {})
                await self.ok(
                    client,
                    "wait_operation",
                    {"operation_id": read["operation_id"], "timeout": 5},
                )
            self.assertNotIn(remote, service.jobs)
            own = await self.ok(client, "operation_result", {"operation_id": remote})
            self.assertTrue(own["recovered"])
            self.assertEqual(own["status"], "succeeded")
            await self.assert_like_unknown(client, local)
        async with self.client(service, remote=False) as client:
            mine = await self.ok(client, "operation_result", {"operation_id": local})
            self.assertTrue(mine["recovered"])
            self.assertEqual(mine["status"], "outcome_unknown")
        self.assertEqual([k for k, _ in self.backend.calls].count("book"), 2)


class WatchBookingTests(ToolCase):
    async def test_a_remote_watch_starts_its_own_and_never_returns_the_hosts(self):
        reference = ROW["reference_number"]
        host = self.service.watch_booking(reference)
        async with self.client(self.service, remote=True) as client:
            mine = await self.ok(
                client, "watch_booking", {"reference_number": reference}
            )
            again = await self.ok(
                client, "watch_booking", {"reference_number": reference}
            )
            await self.assert_like_unknown(
                client,
                host["operation_id"],
                tools=(("operation_result", {}), ("cancel_operation", {})),
            )
        self.assertNotEqual(mine["operation_id"], host["operation_id"])
        self.assertNotIn(host["operation_id"], json.dumps([mine, again]))
        self.assertEqual(again["operation_id"], mine["operation_id"])
        self.assertTrue(self.service.jobs[mine["operation_id"]].remote)
        self.assertFalse(self.service.jobs[host["operation_id"]].remote)
        self.assertFalse(self.service.jobs[host["operation_id"]].terminal)
        async with self.client(self.service, remote=False) as client:
            repeat = await self.ok(
                client, "watch_booking", {"reference_number": reference}
            )
        self.assertEqual(repeat["operation_id"], host["operation_id"])
        watches = [j for j in self.service.jobs.values() if j.kind == "booking_watch"]
        self.assertEqual(len(watches), 2)

    async def test_four_host_watches_give_a_remote_caller_only_the_neutral_cap(self):
        references = [f"BK-20991101-00014{i}" for i in range(4)]
        host = [self.service.watch_booking(reference) for reference in references]
        async with self.client(self.service, remote=True) as client:
            for reference in (references[0], "BK-20991101-000199"):
                with self.subTest(reference=reference):
                    failed, text, _ = await self.call(
                        client, "watch_booking", {"reference_number": reference}
                    )
                    self.assertTrue(failed)
                    # The SDK prefixes the tool name; the rest is the message.
                    self.assertEqual(
                        text.removeprefix("Error executing tool watch_booking: "),
                        CAP,
                    )
                    for job in host:
                        self.assertNotIn(job["operation_id"], text)
        with remote_origin(), self.assertRaises(AgentError) as caught:
            self.service.watch_booking(references[0])
        self.assertEqual(str(caught.exception), CAP)
        self.assertFalse(any(job.remote for job in self.service.jobs.values()))
        # The host's own repeat still returns its watcher, cap or no cap.
        self.assertEqual(
            self.service.watch_booking(references[0])["operation_id"],
            host[0]["operation_id"],
        )


if __name__ == "__main__":
    unittest.main()
