"""Who started each write, kept in SQLite (action_origins): upstream compatibility,
history filtering, marking earlier writes, atomic recording, and the install.sh
step that keeps a hosted copy's unknown history private. Only temporary SQLite
files, fixtures and local subprocesses; nothing contacts FBS or Microsoft, needs
root or changes the system."""

import importlib.util
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, nullcontext
from importlib.machinery import SourceFileLoader
from pathlib import Path

from dotenv import dotenv_values

from sagasu.action_journal import ActionJournal, JournalError
from sagasu.mcp_service import AgentError, AgentService, Job
from tests.test_mcp import CONFIG, ROOM, FakeBackend
from tests.test_remote import remote_origin

REPO = Path(__file__).resolve().parents[1]
KIT = REPO / "deploy" / "server"
INSTALL = (KIT / "install.sh").read_text(encoding="utf-8")
PYTHON = (
    str(REPO / ".venv" / "bin" / "python")
    if (REPO / ".venv" / "bin" / "python").exists()
    else sys.executable
)

# Upstream (origin/main) sagasu/action_journal.py, verbatim: its two tables and
# its positional insert. Upstream code must keep working on our databases.
UPSTREAM_ACTIONS = "CREATE TABLE IF NOT EXISTS actions (operation_id TEXT PRIMARY KEY, review_id TEXT UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, summary TEXT NOT NULL, result TEXT, error_code TEXT)"
UPSTREAM_RECONCILIATIONS = "CREATE TABLE IF NOT EXISTS reconciliations (operation_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, evidence TEXT NOT NULL)"
UPSTREAM_INSERT = "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?)"
UPSTREAM_COLUMNS = [
    "operation_id",
    "review_id",
    "kind",
    "status",
    "created_at",
    "updated_at",
    "summary",
    "result",
    "error_code",
]
SLOT = {
    "room": ROOM["name"],
    "date": CONFIG["date"],
    "start_time": CONFIG["start_time"],
    "end_time": CONFIG["end_time"],
}
UNKNOWN = "Unknown or expired operation"
AT_HOST = "started at the host"


def slot(room=ROOM["name"]):
    return {**SLOT, "room": room}


def upstream_row(
    operation_id, *, status="outcome_unknown", created_at=100.0, room=ROOM["name"]
):
    """One booking row exactly as upstream's reserve()/update()/recover() leave it."""
    return (
        operation_id,
        f"review-{operation_id}",
        "book",
        status,
        created_at,
        created_at,
        json.dumps(slot(room)),
        None,
        "ProcessInterrupted" if status == "outcome_unknown" else None,
    )


def connect(path):
    connection = sqlite3.connect(path)
    return closing(connection)


def insert_upstream(path, *rows):
    """Rows written the way upstream code writes them: positionally."""
    with connect(path) as db, db:
        db.executemany(UPSTREAM_INSERT, rows)


def build_upstream_database(path):
    """A history file as upstream's ActionJournal creates it: no action_origins."""
    with connect(path) as db, db:
        db.execute(UPSTREAM_ACTIONS)
        db.execute("PRAGMA user_version=1")
        db.execute(UPSTREAM_RECONCILIATIONS)
    os.chmod(path, 0o600)


def tables(path):
    with connect(path) as db:
        return {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def origins(path):
    with connect(path) as db:
        return dict(db.execute("SELECT operation_id, origin FROM action_origins"))


def action_ids(path):
    with connect(path) as db:
        return {row[0] for row in db.execute("SELECT operation_id FROM actions")}


def job(operation_id, *, remote=False, created_at=200.0):
    return Job(operation_id, "book", created_at, updated_at=created_at, remote=remote)


def load_kit(name):
    """A deploy-kit script (no .py suffix) as a module, without running main."""
    loader = SourceFileLoader("kit_origins_" + name.replace("-", "_"), str(KIT / name))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader)
    )
    loader.exec_module(module)
    return module


class JournalCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / "actions.sqlite3"
        self.journal = ActionJournal(self.path)

    def ids(self, page):
        return [action["operation_id"] for action in page["actions"]]


class UpstreamCompatibilityTests(JournalCase):
    def test_new_database_keeps_upstreams_nine_columns_in_order(self):
        self.journal.history()  # creates the database
        with connect(self.path) as db:
            info = db.execute("PRAGMA table_info(actions)").fetchall()
            created = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='actions'"
            ).fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual([column[1] for column in info], UPSTREAM_COLUMNS)
        # Same types, NOT NULLs and key as a table upstream itself created.
        upstream = self.folder / "upstream.sqlite3"
        build_upstream_database(upstream)
        with connect(upstream) as db:
            self.assertEqual(db.execute("PRAGMA table_info(actions)").fetchall(), info)
        self.assertEqual(created, UPSTREAM_ACTIONS.replace(" IF NOT EXISTS", ""))
        # Upstream refuses any other schema version, so it must stay 1.
        self.assertEqual(version, 1)
        self.assertIn("action_origins", tables(self.path))

    def test_upstream_positional_insert_works_and_its_row_counts_as_local(self):
        self.journal.reserve(job("ours-local", created_at=200.0), "r1", slot("A"))
        insert_upstream(self.path, upstream_row("legacy", created_at=100.0))
        saved = self.journal.get("legacy")
        self.assertEqual(saved["status"], "outcome_unknown")
        self.assertEqual(saved["summary"], SLOT)
        self.assertIsNone(saved["origin"])
        self.assertIsNone(self.journal.get(review_id="review-legacy")["origin"])
        self.assertEqual(self.journal.get("ours-local")["origin"], "local")
        self.assertIn("legacy", self.ids(self.journal.history(0, 50, origin="local")))
        self.assertNotIn(
            "legacy", self.ids(self.journal.history(0, 50, origin="remote"))
        )
        self.assertEqual(
            self.ids(self.journal.history(0, 50)), ["ours-local", "legacy"]
        )
        self.assertNotIn("legacy", origins(self.path))  # nothing invented

    def test_upstream_schema_database_opened_by_new_journal_gains_origins(self):
        build_upstream_database(self.path)
        insert_upstream(self.path, upstream_row("legacy", status="outcome_unknown"))
        with connect(self.path) as db, db:
            db.execute(
                "INSERT INTO reconciliations VALUES(?,?,?)",
                ("legacy", 150.0, json.dumps({"state": "unresolved"})),
            )
        self.assertEqual(tables(self.path), {"actions", "reconciliations"})

        saved = self.journal.get("legacy")
        self.assertIsNone(saved["origin"])
        self.assertEqual(saved["reconciliation"]["state"], "unresolved")
        self.assertTrue(saved["reconciliation_required"])
        self.assertEqual(
            tables(self.path), {"actions", "reconciliations", "action_origins"}
        )
        page = self.journal.history(0, 50, origin="local")
        self.assertEqual((self.ids(page), page["total"]), (["legacy"], 1))
        self.assertEqual(self.journal.history(0, 50, origin="remote")["total"], 0)
        # New writes on the old file record their origin as usual.
        self.journal.reserve(job("ours", remote=True), "r2", slot("Another room"))
        self.assertEqual(origins(self.path), {"ours": "remote"})
        with connect(self.path) as db:
            info = db.execute("PRAGMA table_info(actions)").fetchall()
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual([column[1] for column in info], UPSTREAM_COLUMNS)


class UpstreamRowServiceTests(unittest.IsolatedAsyncioTestCase):
    """A row upstream code wrote, seen through a freshly started AgentService."""

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "actions.sqlite3"
        self.backend = FakeBackend()
        ActionJournal(self.path).history()  # created by the new code
        insert_upstream(self.path, upstream_row("legacy-op", status="outcome_unknown"))

    def make(self):
        service = AgentService(
            backend=self.backend, journal=ActionJournal(self.path), clock=lambda: 1000.0
        )
        self.addAsyncCleanup(service.close)
        return service

    async def submit_slot(self, service, *, remote):
        with remote_origin() if remote else nullcontext():
            search = service.search(CONFIG)
            await service.get(search["operation_id"]).task
            review = service.review_booking(
                search["operation_id"],
                ROOM["name"],
                "Project meeting",
                "friend@scis.smu.edu.sg",
            )
            return service.submit(review["review_id"])

    def books(self):
        return [kind for kind, _ in self.backend.calls].count("book")

    async def test_remote_caller_cannot_list_read_or_reconcile_an_upstream_row(self):
        service = self.make()
        with remote_origin():
            history = service.action_history(0, 50)
            self.assertEqual((history["actions"], history["total"]), ([], 0))
            with self.assertRaises(AgentError) as never_existed:
                service.get("no-such-operation")
            for call in (service.get, service.reconcile_operation):
                with self.subTest(call=call.__name__):
                    with self.assertRaises(AgentError) as caught:
                        call("legacy-op")
                    self.assertIn(UNKNOWN, str(caught.exception))
                    # Indistinguishable from an ID that never existed.
                    self.assertEqual(
                        str(caught.exception), str(never_existed.exception)
                    )
        # The person at the host sees it, as an action of theirs.
        everything = service.action_history(0, 50)
        self.assertEqual([a["origin"] for a in everything["actions"]], [None])
        recovered = service.get("legacy-op")
        self.assertTrue(recovered.recovered)
        self.assertFalse(recovered.remote)
        self.assertEqual(recovered.status, "outcome_unknown")

    async def test_remote_repeat_of_an_upstream_rows_slot_is_refused_without_its_id(
        self,
    ):
        service = self.make()
        with self.assertRaises((AgentError, JournalError)) as caught:
            await self.submit_slot(service, remote=True)
        self.assertIsInstance(caught.exception, JournalError)  # the journal refused
        self.assertIn(AT_HOST, str(caught.exception))
        self.assertNotIn("legacy-op", str(caught.exception))
        self.assertEqual(self.books(), 0)
        self.assertEqual(action_ids(self.path), {"legacy-op"})
        self.assertEqual(origins(self.path), {})
        # The person at the host is told which operation to reconcile.
        with self.assertRaises(JournalError) as host:
            await self.submit_slot(service, remote=False)
        self.assertIn("legacy-op", str(host.exception))
        self.assertNotIn(AT_HOST, str(host.exception))
        self.assertEqual(self.books(), 0)


class ReserveBlockTests(JournalCase):
    """The duplicate-write block at the journal level, by who asks and who began."""

    def test_block_names_the_operation_only_to_whoever_may_address_it(self):
        self.journal.history()  # created by the new code
        insert_upstream(self.path, upstream_row("legacy", status="running"))
        self.journal.reserve(job("host-op"), "r-host", slot("Room H"))
        self.journal.reserve(job("agent-op", remote=True), "r-agent", slot("Room R"))
        cases = [
            # (room of the earlier write, remote asker, name shown?)
            (ROOM["name"], True, False),  # legacy row counts as the host's
            ("Room H", True, False),
            ("Room R", True, True),
            (ROOM["name"], False, True),
            ("Room H", False, True),
            ("Room R", False, True),
        ]
        earlier = {ROOM["name"]: "legacy", "Room H": "host-op", "Room R": "agent-op"}
        for number, (room, remote, named) in enumerate(cases):
            with self.subTest(room=room, remote=remote):
                with self.assertRaises(JournalError) as caught:
                    self.journal.reserve(
                        job(f"new-{number}", remote=remote), f"rn-{number}", slot(room)
                    )
                message = str(caught.exception)
                self.assertEqual(earlier[room] in message, named)
                self.assertEqual(AT_HOST in message, not named)
        self.assertEqual(action_ids(self.path), {"legacy", "host-op", "agent-op"})
        self.assertEqual(origins(self.path), {"host-op": "local", "agent-op": "remote"})

    def test_origin_comes_from_job_remote_and_defaults_to_local(self):
        self.journal.reserve(job("a", remote=True), "ra", slot("A"))
        self.journal.reserve(job("b", remote=False), "rb", slot("B"))
        truthy = job("c")
        truthy.remote = 1  # only True itself counts as remote
        self.journal.reserve(truthy, "rc", slot("C"))
        self.assertEqual(
            origins(self.path), {"a": "remote", "b": "local", "c": "local"}
        )
        self.assertEqual(self.journal.get("a")["origin"], "remote")
        self.assertEqual(self.journal.get(review_id="rb")["origin"], "local")


class HistoryOriginTests(JournalCase):
    def setUp(self):
        super().setUp()
        self.journal.history()
        insert_upstream(self.path, upstream_row("legacy", created_at=100.0))
        self.journal.reserve(job("local", created_at=200.0), "rl", slot("L"))
        self.journal.reserve(
            job("remote", remote=True, created_at=300.0), "rr", slot("R")
        )

    def test_each_filter_counts_and_lists_its_own_rows(self):
        expected = {
            None: ["remote", "local", "legacy"],
            "remote": ["remote"],
            "local": ["local", "legacy"],
        }
        for origin, ids in expected.items():
            with self.subTest(origin=origin):
                page = self.journal.history(0, 50, origin=origin)
                self.assertEqual(self.ids(page), ids)
                self.assertEqual(page["total"], len(ids))
                self.assertIsNone(page["next_offset"])
                self.assertFalse(page["automatic_replay"])
                self.assertEqual(
                    {a["operation_id"]: a["origin"] for a in page["actions"]},
                    {
                        key: value
                        for key, value in {
                            "legacy": None,
                            "local": "local",
                            "remote": "remote",
                        }.items()
                        if key in ids
                    },
                )
        # The default is everything.
        self.assertEqual(
            self.journal.history(0, 50), self.journal.history(0, 50, origin=None)
        )

    def test_pages_add_up_to_the_filtered_list_for_every_limit(self):
        for number in range(4):
            insert_upstream(
                self.path,
                upstream_row(f"legacy{number}", created_at=110.0 + 50 * number),
            )
            self.journal.reserve(
                job(f"remote{number}", remote=True, created_at=120.0 + 50 * number),
                f"rr{number}",
                slot(f"R{number}"),
            )
        for origin in (None, "remote", "local"):
            whole = self.journal.history(0, 50, origin=origin)
            for limit in (1, 2, 3, 4):
                with self.subTest(origin=origin, limit=limit):
                    seen, offset, pages = [], 0, 0
                    while offset is not None:
                        page = self.journal.history(offset, limit, origin=origin)
                        self.assertEqual(page["total"], whole["total"])
                        self.assertLessEqual(len(page["actions"]), limit)
                        seen += self.ids(page)
                        expected_next = (
                            offset + limit if offset + limit < whole["total"] else None
                        )
                        self.assertEqual(page["next_offset"], expected_next)
                        offset, pages = page["next_offset"], pages + 1
                        self.assertLess(pages, 50)
                    self.assertEqual(seen, self.ids(whole))
                    self.assertEqual(len(seen), whole["total"])
        self.assertEqual(self.journal.history(0, 50, origin="remote")["total"], 5)
        self.assertEqual(self.journal.history(0, 50, origin="local")["total"], 6)
        # Past the end: nothing, with the same total.
        page = self.journal.history(40, 5, origin="remote")
        self.assertEqual(
            (page["actions"], page["total"], page["next_offset"]), ([], 5, None)
        )

    def test_unknown_origin_is_refused(self):
        for origin in ("bogus", "", "Remote", "LOCAL", "host", 0, False):
            with self.subTest(origin=origin):
                with self.assertRaises(JournalError):
                    self.journal.history(0, 20, origin=origin)


class MarkUnrecordedTests(JournalCase):
    def setUp(self):
        super().setUp()
        self.journal.history()
        insert_upstream(
            self.path,
            upstream_row("legacy1", created_at=100.0, room="L1"),
            upstream_row("legacy2", created_at=110.0, room="L2"),
        )
        self.journal.reserve(job("local", created_at=200.0), "rl", slot("L"))
        self.journal.reserve(
            job("remote", remote=True, created_at=300.0), "rr", slot("R")
        )

    def test_marks_only_rows_without_an_origin_once(self):
        self.assertEqual(self.journal.mark_unrecorded("remote"), 2)
        marked = {
            "legacy1": "remote",
            "legacy2": "remote",
            "local": "local",
            "remote": "remote",
        }
        self.assertEqual(origins(self.path), marked)
        self.assertEqual(self.journal.mark_unrecorded("remote"), 0)
        self.assertEqual(self.journal.mark_unrecorded("local"), 0)
        self.assertEqual(origins(self.path), marked)
        self.assertEqual(self.journal.history(0, 50, origin="remote")["total"], 3)
        self.assertEqual(
            self.ids(self.journal.history(0, 50, origin="local")), ["local"]
        )

    def test_marking_as_local_keeps_remote_rows_remote(self):
        self.assertEqual(self.journal.mark_unrecorded("local"), 2)
        self.assertEqual(
            origins(self.path),
            {
                "legacy1": "local",
                "legacy2": "local",
                "local": "local",
                "remote": "remote",
            },
        )
        self.assertEqual(self.journal.mark_unrecorded("remote"), 0)

    def test_unknown_origin_is_refused_and_changes_nothing(self):
        before = origins(self.path)
        for origin in ("bogus", "", "Remote", None):
            with self.subTest(origin=origin):
                with self.assertRaises(JournalError):
                    self.journal.mark_unrecorded(origin)
        self.assertEqual(origins(self.path), before)

    def test_new_and_upstream_schema_databases_without_unrecorded_rows(self):
        self.assertEqual(
            ActionJournal(self.folder / "new.sqlite3").mark_unrecorded("remote"), 0
        )
        upstream = self.folder / "upstream.sqlite3"
        build_upstream_database(upstream)
        self.assertEqual(ActionJournal(upstream).mark_unrecorded("remote"), 0)
        insert_upstream(upstream, upstream_row("old"))
        self.assertEqual(ActionJournal(upstream).mark_unrecorded("remote"), 1)
        self.assertEqual(origins(upstream), {"old": "remote"})


class MarkUnrecordedServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "actions.sqlite3"
        self.backend = FakeBackend()
        ActionJournal(self.path).history()
        insert_upstream(self.path, upstream_row("legacy-op", status="outcome_unknown"))

    def make(self):
        service = AgentService(
            backend=self.backend, journal=ActionJournal(self.path), clock=lambda: 1000.0
        )
        self.addAsyncCleanup(service.close)
        return service

    async def test_after_marking_remote_the_agent_reconciles_its_earlier_write(self):
        before = self.make()
        with remote_origin():
            with self.assertRaises(AgentError) as caught:
                before.reconcile_operation("legacy-op")
            self.assertIn(UNKNOWN, str(caught.exception))
        await before.close()

        self.assertEqual(ActionJournal(self.path).mark_unrecorded("remote"), 1)
        service = self.make()
        with remote_origin():
            history = service.action_history(0, 50)
            self.assertEqual(
                [(a["operation_id"], a["origin"]) for a in history["actions"]],
                [("legacy-op", "remote")],
            )
            recovered = service.get("legacy-op")
            self.assertTrue(recovered.remote)
            self.assertEqual(recovered.status, "outcome_unknown")
            reconcile = service.reconcile_operation("legacy-op")
            self.assertEqual(reconcile["kind"], "reconcile")
            await service.get(reconcile["operation_id"]).task

    async def test_after_marking_remote_a_remote_repeat_is_told_the_operation(self):
        self.assertEqual(ActionJournal(self.path).mark_unrecorded("remote"), 1)
        service = self.make()
        with remote_origin():
            search = service.search(CONFIG)
            await service.get(search["operation_id"]).task
            review = service.review_booking(
                search["operation_id"],
                ROOM["name"],
                "Project meeting",
                "friend@scis.smu.edu.sg",
            )
            with self.assertRaises(JournalError) as caught:
                service.submit(review["review_id"])
        self.assertIn("legacy-op", str(caught.exception))
        self.assertNotIn(AT_HOST, str(caught.exception))


class AtomicOriginTests(JournalCase):
    def setUp(self):
        super().setUp()
        self.journal.history()

    def test_a_failed_origin_insert_leaves_no_action_row(self):
        with connect(self.path) as db, db:
            db.execute("INSERT INTO action_origins VALUES('fixed-id','local')")
        with self.assertRaises(JournalError):
            self.journal.reserve(job("fixed-id", remote=True), "r1", slot())
        self.assertEqual(action_ids(self.path), set())
        self.assertIsNone(self.journal.get("fixed-id"))
        self.assertIsNone(self.journal.get(review_id="r1"))
        self.assertEqual(origins(self.path), {"fixed-id": "local"})
        # Nothing half-written blocks the slot or the review id afterwards.
        self.journal.reserve(job("other-id", remote=True), "r1", slot())
        self.assertEqual(action_ids(self.path), {"other-id"})

    def test_an_origin_insert_refused_by_the_database_rolls_the_action_back(self):
        with connect(self.path) as db, db:
            db.execute(
                "CREATE TRIGGER refuse_origin BEFORE INSERT ON action_origins "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
        for remote in (False, True):
            with self.subTest(remote=remote):
                with self.assertRaises(JournalError) as caught:
                    self.journal.reserve(job("op", remote=remote), "r-op", slot())
                self.assertNotIn("refused", str(caught.exception))
                self.assertEqual(action_ids(self.path), set())
        with connect(self.path) as db, db:
            db.execute("DROP TRIGGER refuse_origin")
        self.journal.reserve(job("op", remote=True), "r-op", slot())
        self.assertEqual(action_ids(self.path), {"op"})
        self.assertEqual(origins(self.path), {"op": "remote"})

    def test_check_constraint_accepts_only_local_and_remote(self):
        for origin in ("bogus", "Remote", "LOCAL", "", None):
            with self.subTest(origin=origin):
                with connect(self.path) as db:
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(
                            "INSERT INTO action_origins VALUES(?,?)", ("x", origin)
                        )
        with connect(self.path) as db, db:
            db.execute("INSERT INTO action_origins VALUES('x','remote')")
            db.execute("INSERT INTO action_origins VALUES('y','local')")
        self.assertEqual(origins(self.path), {"x": "remote", "y": "local"})


class AtomicOriginServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_origin_insert_never_reaches_fbs_and_the_review_survives(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "actions.sqlite3"
        backend = FakeBackend()
        service = AgentService(
            backend=backend, journal=ActionJournal(path), clock=lambda: 1000.0
        )
        self.addAsyncCleanup(service.close)
        service.action_history()
        with connect(path) as db, db:
            db.execute(
                "CREATE TRIGGER refuse_origin BEFORE INSERT ON action_origins "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
        with remote_origin():
            search = service.search(CONFIG)
            await service.get(search["operation_id"]).task
            review = service.review_booking(
                search["operation_id"],
                ROOM["name"],
                "Project meeting",
                "friend@scis.smu.edu.sg",
            )
            with self.assertRaises(JournalError):
                service.submit(review["review_id"])
        self.assertNotIn("book", [kind for kind, _ in backend.calls])
        self.assertEqual(action_ids(path), set())
        self.assertEqual([job.kind for job in service.jobs.values()], ["search"])
        with connect(path) as db, db:
            db.execute("DROP TRIGGER refuse_origin")
        with remote_origin():
            submitted = service.submit(review["review_id"])
            done = service.get(submitted["operation_id"])
            await done.task
            self.assertEqual(done.status, "succeeded")
        self.assertEqual(origins(path), {submitted["operation_id"]: "remote"})


# install.sh: the step that keeps a hosted copy's unknown history private.
STEP_START = INSTALL.index("mark_history_local() {")
STEP_END = INSTALL.index("# Teammates' copies come back", STEP_START)
STEP = INSTALL[STEP_START:STEP_END]
FUNCTION = re.search(
    r"^mark_history_local\(\) \{\n.*?^\}\n", INSTALL, re.S | re.M
).group(0)
ONE_LINER = re.search(r"-c \\\n\s+'([^']+)'", FUNCTION).group(1)
NAME_TEST = "[[ -f $instance && $copy =~ ^[a-z][a-z0-9]{1,15}$ ]] || continue"
OWNER_CALL = "mark_history_local sagasu /var/lib/sagasu/actions.sqlite3"
COPY_CALL = 'mark_history_local "sagasu-$copy" "/var/lib/sagasu-$copy/actions.sqlite3"'


class InstallStepStaticTests(unittest.TestCase):
    def test_install_sh_parses(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not available")
        result = subprocess.run(
            ["bash", "-n", str(KIT / "install.sh")], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_step_is_defined_once_and_called_for_the_owner_and_every_copy(self):
        self.assertEqual(INSTALL.count("mark_history_local() {"), 1)
        self.assertEqual(
            re.findall(r"^mark_history_local .*$", INSTALL, re.M), [OWNER_CALL]
        )
        loop = re.search(
            r"^for instance in /etc/sagasu/instances/\*; do\n(.*?)^done$",
            STEP,
            re.S | re.M,
        )
        self.assertIsNotNone(loop)
        body = [line.strip() for line in loop.group(1).splitlines()]
        self.assertEqual(body, ["copy=${instance##*/}", NAME_TEST, COPY_CALL])
        # Top level, not inside a first-install-only branch: runs on --update too.
        self.assertIn("\n" + OWNER_CALL + "\n", INSTALL)

    def test_step_runs_as_the_copys_user_with_the_installed_python(self):
        self.assertIn("local user=$1 db=$2", FUNCTION)
        self.assertIn("[[ -f $db ]] || return 0", FUNCTION)
        self.assertIn(
            'runuser -u "$user" -- /opt/sagasu/venv/bin/python -c \\', FUNCTION
        )
        self.assertIn('mark_unrecorded("local")', ONE_LINER)
        self.assertIn("ActionJournal(sys.argv[1])", ONE_LINER)
        self.assertEqual(ONE_LINER.count("mark_unrecorded("), 1)
        self.assertRegex(FUNCTION, r"\n\s+\"\$db\" \|\| true\n\}")

    def test_backup_only_when_none_exists_and_failures_tolerated(self):
        self.assertIn(
            '[[ -e $db.before-origins ]] || cp -p "$db" "$db.before-origins" || true',
            FUNCTION,
        )
        # Every command in the function that can fail is guarded under set -e
        # (a line ending in a backslash continues the runuser command).
        ends = [
            line.strip()
            for line in FUNCTION.splitlines()[1:-1]
            if line.strip() and not line.strip().startswith("local ")
        ]
        commands = [line for line in ends if not line.endswith("\\")]
        self.assertEqual(len(commands), 3, ends)
        for line in commands:
            with self.subTest(line=line):
                self.assertTrue(line.endswith(("|| true", "|| return 0")), line)

    def test_step_runs_after_the_new_code_and_before_any_copy_starts(self):
        def at(text, start=0):
            return INSTALL.index(text, start)

        owner = at(OWNER_CALL)
        copies = at(COPY_CALL)
        # The copies are stopped and the new code (with mark_unrecorded) is
        # installed into the venv first.
        self.assertLess(at('systemctl stop sagasu.service "${COPIES[@]}"'), owner)
        self.assertLess(
            at(
                'pip install --quiet --force-reinstall --no-deps "/opt/sagasu/app[hosted]"'
            ),
            owner,
        )
        self.assertLess(at("systemctl restart sagasu-gateway.service"), owner)
        restart_loop = at("# Teammates' copies come back")
        for later in (
            restart_loop,
            at('systemctl start "$unit"', restart_loop),
            at("if [[ $LEAVE_STOPPED -eq 1 ]]; then", restart_loop),
            at("systemctl start sagasu.service", restart_loop),
        ):
            self.assertLess(owner, later)
            self.assertLess(copies, later)

    def test_paths_users_and_names_match_the_kit(self):
        ctl = load_kit("sagasu-ctl")
        owner, alice = ctl.Copy(), ctl.Copy("alice")
        self.assertEqual(
            OWNER_CALL.split()[1:], [owner.account, f"{owner.state}/actions.sqlite3"]
        )
        self.assertEqual(
            dotenv_values(KIT / "sagasu.env.example")["SAGASU_JOURNAL_PATH"],
            f"{owner.state}/actions.sqlite3",
        )
        expanded = COPY_CALL.replace("$copy", "alice")
        self.assertEqual(
            shlex.split(expanded)[1:],
            [alice.account, f"{alice.state}/actions.sqlite3"],
        )
        ctl_text = (KIT / "sagasu-ctl").read_text(encoding="utf-8")
        self.assertIn('f"SAGASU_JOURNAL_PATH={copy.state}/actions.sqlite3"', ctl_text)
        self.assertIn("=~ ^" + ctl.NAME.pattern + "$ ]]", NAME_TEST)


class InstallOneLinerTests(JournalCase):
    def run_one_liner(self, path):
        return subprocess.run(
            [PYTHON, "-c", ONE_LINER, str(path)],
            cwd=REPO,
            env={**os.environ, "PYTHONPATH": str(REPO)},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_one_liner_marks_rows_and_prints_only_when_it_marked_any(self):
        build_upstream_database(self.path)
        insert_upstream(
            self.path,
            upstream_row("old1", room="A"),
            upstream_row("old2", room="B", status="succeeded"),
        )
        first = self.run_one_liner(self.path)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            first.stdout, f"{self.path}: 2 earlier write(s) kept private to the host\n"
        )
        self.assertEqual(origins(self.path), {"old1": "local", "old2": "local"})
        second = self.run_one_liner(self.path)
        self.assertEqual((second.returncode, second.stdout), (0, ""), second.stderr)

    def test_one_liner_is_silent_on_a_history_with_every_origin_known(self):
        self.journal.reserve(job("host-op"), "r1", slot("A"))
        self.journal.reserve(job("agent-op", remote=True), "r2", slot("B"))
        result = self.run_one_liner(self.path)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(origins(self.path), {"host-op": "local", "agent-op": "remote"})


@unittest.skipIf(shutil.which("bash") is None, "bash is not available")
class InstallStepRunTests(unittest.TestCase):
    """install.sh's step itself, its paths moved into a temporary root, runuser
    recorded and then run as the current user with the repo's Python."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.assertNotRegex(str(self.root), r"\s")
        self.log = self.root / "log"
        (self.root / "etc/sagasu/instances").mkdir(parents=True)

    def db(self, state):
        return self.root / "var/lib" / state / "actions.sqlite3"

    def instance(self, name, *, directory=False):
        path = self.root / "etc/sagasu/instances" / name
        path.mkdir() if directory else path.write_text("HOST=x.example.test\n")

    def history(self, state, *rows, upstream=True):
        path = self.db(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        if upstream:
            build_upstream_database(path)
        else:
            ActionJournal(path).history()
        insert_upstream(path, *rows)
        return path

    def run_step(self, runuser_status=0):
        root = str(self.root)
        body = re.sub(
            r"/var/lib/|/etc/sagasu/", lambda found: root + found.group(0), STEP
        ).replace("/opt/sagasu/venv/bin/python", shlex.quote(PYTHON))
        script = "\n".join(
            [
                "set -euo pipefail",
                f"LOG={shlex.quote(str(self.log))}",
                f"RUNUSER_STATUS={runuser_status}",
                'runuser() { printf "runuser %s %s\\n" "$2" "${!#}" >>"$LOG";'
                " [[ $1 == -u && $3 == -- ]] || return 97;"
                ' [[ $RUNUSER_STATUS == 0 ]] || return "$RUNUSER_STATUS";'
                ' shift 3; "$@"; }',
                body,
                "echo reached-the-restart",
                "",
            ]
        )
        self.log.write_text("")
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=REPO,
            env={**os.environ, "PYTHONPATH": str(REPO)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result, self.log.read_text().splitlines()

    def test_update_keeps_unknown_history_private_and_known_remote_history_visible(
        self,
    ):
        path = self.history("sagasu", upstream_row("legacy-host-write"))
        journal = ActionJournal(path)
        journal.reserve(job("remote-write", remote=True), "remote-review", slot("R"))
        journal.recover(200)

        result, _ = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        service = AgentService(backend=FakeBackend(), journal=ActionJournal(path))
        with remote_origin():
            history = service.action_history()
            self.assertEqual(history["total"], 1)
            self.assertEqual(
                [row["operation_id"] for row in history["actions"]], ["remote-write"]
            )
            with self.assertRaises(AgentError):
                service.get("legacy-host-write")
            with self.assertRaises(AgentError):
                service.reconcile_operation("legacy-host-write")
            self.assertTrue(service.get("remote-write").remote)
        self.assertEqual(service.action_history()["total"], 2)

    def test_marks_owner_and_valid_copies_once_with_one_backup_each(self):
        owner = self.history(
            "sagasu", upstream_row("o1", room="A"), upstream_row("o2", room="B")
        )
        alice = self.history("sagasu-alice", upstream_row("a1"), upstream=False)
        ActionJournal(alice).reserve(job("a-host"), "rh", slot("Host room"))
        alice_backup = Path(f"{alice}.before-origins")
        alice_backup.write_bytes(b"an older backup")
        self.instance("alice")
        self.instance("bob")  # no history yet: skipped quietly
        skipped = {}
        for name in ("Bad1", "x", "a" * 17, "carol.env", "dave"):
            self.instance(name, directory=name == "dave")
            skipped[name] = self.history(f"sagasu-{name}", upstream_row("s1"))

        result, log = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            log,
            [f"runuser sagasu {owner}", f"runuser sagasu-alice {alice}"],
        )
        self.assertEqual(
            result.stdout.splitlines(),
            [
                f"{owner}: 2 earlier write(s) kept private to the host",
                f"{alice}: 1 earlier write(s) kept private to the host",
                "reached-the-restart",
            ],
        )
        self.assertEqual(origins(owner), {"o1": "local", "o2": "local"})
        self.assertEqual(origins(alice), {"a1": "local", "a-host": "local"})
        # The owner's backup is the file as it was, before origins existed.
        owner_backup = Path(f"{owner}.before-origins")
        self.assertEqual(tables(owner_backup), {"actions", "reconciliations"})
        self.assertEqual(action_ids(owner_backup), {"o1", "o2"})
        self.assertEqual(alice_backup.read_bytes(), b"an older backup")
        for name, path in skipped.items():
            with self.subTest(skipped=name):
                self.assertEqual(tables(path), {"actions", "reconciliations"})
                self.assertFalse(Path(f"{path}.before-origins").exists())
        self.assertFalse((self.root / "var/lib/sagasu-bob").exists())

        again, log = self.run_step()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, "reached-the-restart\n")
        self.assertEqual(len(log), 2)
        self.assertEqual(tables(owner_backup), {"actions", "reconciliations"})
        self.assertEqual(alice_backup.read_bytes(), b"an older backup")

    def test_no_history_and_no_copies_is_a_quiet_no_op(self):
        result, log = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, log), ("reached-the-restart\n", []))

    def test_a_failing_mark_never_stops_the_installer(self):
        owner = self.history("sagasu", upstream_row("o1"))
        result, log = self.run_step(runuser_status=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "reached-the-restart\n")
        self.assertEqual(log, [f"runuser sagasu {owner}"])
        self.assertEqual(tables(owner), {"actions", "reconciliations"})
        self.assertTrue(Path(f"{owner}.before-origins").exists())

    @unittest.skipIf(os.geteuid() == 0, "root ignores the read-only folder")
    def test_a_failing_backup_and_unwritable_history_never_stop_the_installer(self):
        owner = self.history("sagasu", upstream_row("o1"))
        folder = owner.parent
        folder.chmod(0o555)
        self.addCleanup(folder.chmod, 0o700)
        result, log = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "reached-the-restart\n")
        self.assertEqual(log, [f"runuser sagasu {owner}"])
        self.assertFalse(Path(f"{owner}.before-origins").exists())
        folder.chmod(0o700)
        self.assertEqual(tables(owner), {"actions", "reconciliations"})


if __name__ == "__main__":
    unittest.main()
