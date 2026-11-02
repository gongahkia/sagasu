"""Private, durable action outcomes. Never store executable payloads or replay them."""

import json
import os
import re
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


SUMMARY_KEYS = {"room", "date", "start_time", "end_time", "reference_number", "date_time", "fbs_status"}
RESULT_KEYS = {"status", "reference_number", "fbs_status", "task_status", "booking_status_verified"}


def safe_fields(value, allowed):
    result = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str):
            item = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted-email>", item)
            result[key] = item[:500]
    return result


def action_summary(kind, payload):
    if kind == "book":
        return safe_fields({"room": payload.room, "date": payload.search.date,
            "start_time": payload.search.start_time, "end_time": payload.search.end_time}, SUMMARY_KEYS)
    return safe_fields(payload[0] if kind == "cancel" else payload, SUMMARY_KEYS)


class ActionJournal:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()

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
                raise JournalError("Unsupported action history schema; no action was submitted.")
            connection.execute("CREATE TABLE IF NOT EXISTS actions (operation_id TEXT PRIMARY KEY, review_id TEXT UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, summary TEXT NOT NULL, result TEXT, error_code TEXT)")
            connection.execute("PRAGMA user_version=1")
            with connection:
                yield connection
        except JournalError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise JournalError("Action history is unavailable. Check private storage before submitting another action.") from None
        finally:
            if connection is not None:
                connection.close()

    def reserve(self, job, review_id, summary):
        with self._db() as db:
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?)", (
                job.id, review_id, job.kind, "queued", job.created_at, job.updated_at,
                json.dumps(safe_fields(summary, SUMMARY_KEYS)), None, None,
            ))

    def update(self, job, *, status=None, result=None):
        with self._db() as db:
            cursor = db.execute("UPDATE actions SET status=?, updated_at=?, result=COALESCE(?,result), error_code=? WHERE operation_id=?", (
                status or job.status, job.updated_at,
                json.dumps(safe_fields(result, RESULT_KEYS)) if result is not None else None,
                (job.error or {}).get("code"), job.id,
            ))
            if cursor.rowcount != 1:
                raise JournalError("Action history entry is missing; no further action may be submitted.")

    def recover(self, now):
        """Only called under the server's profile lock. Never resume a job."""
        with self._db() as db:
            db.execute("UPDATE actions SET status=CASE status WHEN 'queued' THEN 'cancelled' ELSE 'outcome_unknown' END, updated_at=?, error_code='ProcessInterrupted' WHERE status IN ('queued','running')", (now,))

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
            row = db.execute("SELECT * FROM actions WHERE " + ("review_id=?" if review_id is not None else "operation_id=?"), (review_id if review_id is not None else operation_id,)).fetchone()
            return self._record(row)

    def history(self, offset=0, limit=20):
        if isinstance(offset, bool) or isinstance(limit, bool) or not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 50:
            raise JournalError("Use offset >= 0 and limit between 1 and 50.")
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            rows = db.execute("SELECT * FROM actions ORDER BY created_at DESC, operation_id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return {"actions": [self._record(row) for row in rows], "total": total,
                "next_offset": offset + limit if offset + limit < total else None,
                "automatic_replay": False}
