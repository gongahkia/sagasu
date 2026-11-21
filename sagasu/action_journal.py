"""Private, durable action outcomes. Never store executable payloads or replay them."""

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class JournalError(RuntimeError):
    pass


VERIFIED_ACTION = ContextVar("sagasu_verified_action", default=None)


def checkpoint_verified(result):
    """Called after positive FBS verification, before optional receipt capture."""
    callback = VERIFIED_ACTION.get()
    if callback is not None:
        callback(result)


SUMMARY_KEYS = {
    "room",
    "date",
    "start_time",
    "end_time",
    "reference_number",
    "date_time",
    "fbs_status",
    "verification_fields",
    "verification_digest",
}
RESULT_KEYS = {
    "status",
    "reference_number",
    "fbs_status",
    "task_status",
    "booking_status_verified",
}


def safe_fields(value, allowed):
    result = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str):
            item = re.sub(
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                "<redacted-email>",
                item,
            )
            result[key] = item[:500]
    return result


def action_summary(kind, payload):
    if kind == "amend":
        return safe_fields(
            {
                **payload["target"],
                "verification_fields": payload["verification_fields"],
                "verification_digest": payload["verification_digest"],
            },
            SUMMARY_KEYS,
        )
    if kind == "book":
        return safe_fields(
            {
                "room": payload.room,
                "date": payload.search.date,
                "start_time": payload.search.start_time,
                "end_time": payload.search.end_time,
            },
            SUMMARY_KEYS,
        )
    return safe_fields(payload[0] if kind == "cancel" else payload, SUMMARY_KEYS)


class ActionJournal:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()

    def comparison_key(self):
        """Keep keyed comparison fingerprints useful after restart, without purpose text."""
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS comparison_metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM comparison_metadata WHERE name='amendment_key'"
            ).fetchone()
            if row is None:
                if db.execute(
                    "SELECT 1 FROM actions WHERE kind='amend' LIMIT 1"
                ).fetchone():
                    raise JournalError(
                        "The amendment comparison key is missing from existing history; restore the private history backup instead of resetting it."
                    )
                key = secrets.token_bytes(32)
                db.execute(
                    "INSERT INTO comparison_metadata VALUES('amendment_key',?)",
                    (key.hex(),),
                )
                return key
            try:
                key = bytes.fromhex(row[0])
            except (TypeError, ValueError):
                raise JournalError(
                    "The amendment comparison key is unreadable; no reset was performed."
                ) from None
            if len(key) != 32:
                raise JournalError(
                    "The amendment comparison key is invalid; no reset was performed."
                )
            return key

    @contextmanager
    def _db(self):
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            self.path.chmod(0o600)
            connection = sqlite3.connect(self.path, timeout=2)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise JournalError(
                    "Unsupported action history schema; no action was submitted."
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS actions (operation_id TEXT PRIMARY KEY, review_id TEXT UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, summary TEXT NOT NULL, result TEXT, error_code TEXT)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reconciliations (operation_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, evidence TEXT NOT NULL)"
            )
            # Who started each action: the person at the host ("local") or a
            # remote caller ("remote"). A separate table, so older code that
            # inserts into actions without naming columns keeps working.
            # Actions recorded before it have no row and count as local.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS action_origins (operation_id TEXT PRIMARY KEY, origin TEXT NOT NULL CHECK (origin IN ('local','remote')))"
            )
            with connection:
                yield connection
        except JournalError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise JournalError(
                "Action history is unavailable. Check private storage before submitting another action."
            ) from None
        finally:
            if connection is not None:
                connection.close()

    def reserve(self, job, review_id, summary):
        origin = "remote" if getattr(job, "remote", False) is True else "local"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            from .reconciliation import identity

            wanted = identity(job.kind, summary)
            for row in db.execute(
                "SELECT * FROM actions WHERE kind=? AND status IN ('queued','running','outcome_unknown')",
                (job.kind,),
            ).fetchall():
                previous = self._with_reconciliation(db, self._record(row))
                if (
                    wanted is not None
                    and identity(job.kind, previous["summary"]) == wanted
                ):
                    # Even verified reconciliation must not race an active write.
                    if previous["status"] != "outcome_unknown" or not (
                        previous.get("reconciliation") or {}
                    ).get("action_outcome_verified"):
                        if origin == "remote" and previous["origin"] != "remote":
                            raise JournalError(
                                "A matching action started at the host is active or unresolved. The person at the host must reconcile it before another can be submitted."
                            )
                        raise JournalError(
                            "A matching action is active or unresolved. Reconcile operation "
                            + previous["operation_id"]
                            + " before preparing another submission."
                        )
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job.id,
                    review_id,
                    job.kind,
                    "queued",
                    job.created_at,
                    job.updated_at,
                    json.dumps(safe_fields(summary, SUMMARY_KEYS)),
                    None,
                    None,
                ),
            )
            db.execute("INSERT INTO action_origins VALUES(?,?)", (job.id, origin))

    def update(self, job, *, status=None, result=None):
        with self._db() as db:
            cursor = db.execute(
                "UPDATE actions SET status=?, updated_at=?, result=COALESCE(?,result), error_code=? WHERE operation_id=?",
                (
                    status or job.status,
                    job.updated_at,
                    (
                        json.dumps(safe_fields(result, RESULT_KEYS))
                        if result is not None
                        else None
                    ),
                    (job.error or {}).get("code"),
                    job.id,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(
                    "Action history entry is missing; no further action may be submitted."
                )

    def recover(self, now):
        """Only called under the server's profile lock. Never resume a job."""
        with self._db() as db:
            db.execute(
                "UPDATE actions SET status=CASE status WHEN 'queued' THEN 'cancelled' ELSE 'outcome_unknown' END, updated_at=?, error_code='ProcessInterrupted' WHERE status IN ('queued','running')",
                (now,),
            )

    @staticmethod
    def _record(row):
        if row is None:
            return None
        record = dict(row)
        record["summary"] = json.loads(record["summary"])
        record["result"] = json.loads(record["result"]) if record["result"] else None
        record["reconciliation_required"] = record["status"] == "outcome_unknown"
        record["replayed"] = False
        return record

    def get(self, operation_id=None, *, review_id=None):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM actions WHERE "
                + ("review_id=?" if review_id is not None else "operation_id=?"),
                (review_id if review_id is not None else operation_id,),
            ).fetchone()
            return self._with_reconciliation(db, self._record(row))

    @staticmethod
    def _with_reconciliation(db, record):
        if record is None:
            return None
        origin = db.execute(
            "SELECT origin FROM action_origins WHERE operation_id=?",
            (record["operation_id"],),
        ).fetchone()
        record["origin"] = origin[0] if origin else None
        row = db.execute(
            "SELECT observed_at,evidence FROM reconciliations WHERE operation_id=?",
            (record["operation_id"],),
        ).fetchone()
        record["reconciliation"] = (
            {**json.loads(row[1]), "observed_at": row[0]} if row else None
        )
        record["reconciliation_required"] = record[
            "status"
        ] == "outcome_unknown" and not (record["reconciliation"] or {}).get(
            "action_outcome_verified", False
        )
        return record

    def record_reconciliation(self, operation_id, result, now):
        allowed = {
            "confirmed",
            "pending_confirmation",
            "cancelled",
            "not_cancelled",
            "approved",
            "amended",
            "matching_booking_found",
            "not_found",
            "unresolved",
            "ambiguous",
        }
        state = result.get("state")
        if state not in allowed:
            raise JournalError(
                "The reconciliation evidence is invalid; the original outcome was preserved."
            )
        data = safe_fields(
            result,
            {
                "state",
                "reference_number",
                "fbs_status",
                "source_complete",
                "action_outcome_verified",
                "booking_status_verified",
            },
        )
        # A caller cannot assert verification for an absence or ambiguous match.
        data["action_outcome_verified"] = (
            state
            in {
                "confirmed",
                "pending_confirmation",
                "cancelled",
                "not_cancelled",
                "approved",
                "amended",
            }
            and result.get("action_outcome_verified") is True
        )
        # Only a verified "did not take effect" makes a retry safe.
        data["safe_to_retry"] = (
            state == "not_cancelled"
            and data["action_outcome_verified"]
            and result.get("safe_to_retry") is True
        )
        with self._db() as db:
            original = db.execute(
                "SELECT kind FROM actions WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if not original:
                raise JournalError("The original action history entry is unavailable.")
            if state == "not_cancelled" and original[0] != "cancel":
                # Only a cancel can be shown not to have taken effect.
                data["action_outcome_verified"] = data["safe_to_retry"] = False
            db.execute(
                "INSERT INTO reconciliations VALUES(?,?,?) ON CONFLICT(operation_id) DO UPDATE SET observed_at=excluded.observed_at,evidence=excluded.evidence",
                (operation_id, now, json.dumps(data)),
            )
        return {**data, "observed_at": now}

    def unresolved_count(self):
        """Count unresolved writes without exporting any action metadata."""
        with self._db() as db:
            rows = db.execute(
                "SELECT r.evidence FROM actions a LEFT JOIN reconciliations r "
                "ON r.operation_id=a.operation_id WHERE a.status='outcome_unknown'"
            ).fetchall()
            try:
                return sum(
                    not (json.loads(row[0]) if row[0] else {}).get(
                        "action_outcome_verified", False
                    )
                    for row in rows
                )
            except (ValueError, AttributeError, TypeError):
                raise JournalError(
                    "Action-history evidence could not be read safely."
                ) from None

    def mark_unrecorded(self, origin):
        """Give every action recorded without an origin this one; returns how many.

        Missing origins count as local. The hosted installer records that
        conservative classification; remote classification requires independent
        evidence of the earlier writes' origin.
        """
        if origin not in {"local", "remote"}:
            raise JournalError("Unknown action origin.")
        with self._db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO action_origins SELECT operation_id, ? FROM actions "
                "WHERE operation_id NOT IN (SELECT operation_id FROM action_origins)",
                (origin,),
            )
            return cursor.rowcount

    def history(self, offset=0, limit=20, *, origin=None):
        if (
            isinstance(offset, bool)
            or isinstance(limit, bool)
            or not isinstance(offset, int)
            or not isinstance(limit, int)
            or offset < 0
            or not 1 <= limit <= 50
        ):
            raise JournalError("Use offset >= 0 and limit between 1 and 50.")
        if origin not in {None, "local", "remote"}:
            raise JournalError("Unknown action origin.")
        # Rows without an origin (recorded by older versions) count as local.
        where, params = {
            None: ("", ()),
            "remote": (
                " WHERE operation_id IN (SELECT operation_id FROM action_origins WHERE origin='remote')",
                (),
            ),
            "local": (
                " WHERE operation_id NOT IN (SELECT operation_id FROM action_origins WHERE origin='remote')",
                (),
            ),
        }[origin]
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM actions" + where, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM actions"
                + where
                + " ORDER BY created_at DESC, operation_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return {
                "actions": [
                    self._with_reconciliation(db, self._record(row)) for row in rows
                ],
                "total": total,
                "next_offset": offset + limit if offset + limit < total else None,
                "automatic_replay": False,
            }
