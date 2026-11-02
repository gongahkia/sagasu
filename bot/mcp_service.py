"""Transport-independent, single-user agent operations over the existing FBS client.

Jobs and receipts deliberately live only in memory. Nothing is replayed on restart.
The transport must obtain human approval before calling ``confirm``.
"""

import asyncio
import copy
import secrets
import time
from dataclasses import dataclass, field

from .booking_flow import SMU_EMAIL_RE
from .fbs_client import (
    AuthenticationRequired,
    BookingOutcomeUnknown,
    BookingRequest,
    BookingUnavailable,
    FBSClient,
    SearchConfig,
    execute_booking,
    require_future_booking,
    search_config_from_bot,
)
from .fbs_account import (
    CancellationOutcomeUnknown,
    cancel_booking,
    capture_booking_evidence,
    read_bookings,
    read_task_list,
)
from .observability import event, observed_operation
from .fbs_tasks import TaskApprovalOutcomeUnknown, TaskUnavailable, execute_task_action

WRITE_KINDS = {"book", "cancel", "approve_task"}


class AgentError(ValueError):
    """A safe, actionable error that may be returned to an MCP client."""


class Backend:
    """Use one process/browser lock for Telegram and agent callers alike."""

    @observed_operation("agent.backend")
    async def run(self, kind, payload, progress, notify):
        if kind in {"approve_task", "task_details"}:
            return await execute_task_action(
                payload,
                approve=kind == "approve_task",
                progress=progress,
                auth_notify=notify,
            )
        if kind in {"validate", "book"}:
            return await execute_booking(
                payload, submit=kind == "book", progress=progress, auth_notify=notify
            )
        async with FBSClient(progress=progress, auth_notify=notify) as client:
            if kind == "search":
                return (await client.search(payload)).to_dict()
            if kind == "login":
                await client.ensure_authenticated()
                return {
                    "authenticated": True,
                    "message": "FBS controls verified; session saved.",
                }
            if kind == "bookings":
                return await read_bookings(client)
            if kind == "tasks":
                return await read_task_list(client)
            if kind == "cancel":
                expected, reason = payload
                result = await cancel_booking(client, expected, reason)
                result["reference_number"] = expected["reference_number"]
                if result["status"] == "cancelled":
                    try:
                        result["receipt_png"] = await capture_booking_evidence(
                            client, expected["reference_number"]
                        )
                    except Exception:
                        result["receipt_error"] = (
                            "Cancellation verified; screenshot unavailable."
                        )
                return result
        raise AgentError("Unsupported operation.")


@dataclass
class Job:
    id: str
    kind: str
    created_at: float
    status: str = "queued"
    stage: str = "Waiting for browser"
    updated_at: float = 0
    result: dict | None = None
    error: dict | None = None
    receipt: bytes | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def terminal(self):
        return self.status in {"succeeded", "failed", "cancelled", "outcome_unknown"}

    def public(self):
        return {
            "operation_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "receipt_available": self.receipt is not None,
            "error": self.error,
        }


class AgentService:
    def __init__(
        self,
        *,
        backend=None,
        notify=None,
        clock=time.time,
        limit=32,
        ttl=3600,
        profile=None,
    ):
        self.backend = backend or Backend()
        self.notify = notify
        self.clock, self.limit, self.ttl = clock, limit, ttl
        self.jobs, self.reviews = {}, {}
        self.queue = asyncio.Lock()
        self.closed = False
        self.profile = profile

    def personal_profile(self):
        if self.profile is None:
            raise AgentError("Personal preferences are not configured for this server.")
        return self.profile

    def list_cobookers(self, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 50:
            raise AgentError("Use offset >= 0 and limit between 1 and 50.")
        rows = [
            {"id": key, **person}
            for key, person in self.personal_profile().contacts().items()
        ]
        return {
            "cobookers": rows[offset : offset + limit],
            "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }

    def get_preferences(self):
        from dataclasses import asdict

        preferences = self.personal_profile().preferences()
        return {
            "preferences": preferences,
            "effective_search": asdict(search_config_from_bot(preferences)),
            "timezone": "Asia/Singapore",
            "rolling_start": not preferences.get("start_time"),
            "rolling_date": not preferences.get("date_raw"),
            "affects_existing_reviews": False,
        }

    def prune(self):
        now = self.clock()
        for key, job in list(self.jobs.items()):
            if job.terminal and now - job.updated_at >= self.ttl:
                del self.jobs[key]
        for key, review in list(self.reviews.items()):
            if review["expires_at"] <= now:
                del self.reviews[key]

    def get(self, operation_id):
        self.prune()
        if operation_id not in self.jobs:
            raise AgentError(
                "Unknown or expired operation. Operations disappear on server restart; check My Bookings and Task List before repeating a write."
            )
        return self.jobs[operation_id]

    def start(self, kind, payload=None):
        self.prune()
        if self.closed:
            raise AgentError("Server is shutting down.")
        if len(self.jobs) >= self.limit:
            # Retain active jobs; bound memory even if a client never polls results.
            oldest = next((key for key, job in self.jobs.items() if job.terminal), None)
            if oldest is None:
                raise AgentError(
                    "Operation queue is full. Wait for an operation to finish."
                )
            del self.jobs[oldest]
        job = Job(
            secrets.token_urlsafe(16), kind, self.clock(), updated_at=self.clock()
        )
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, copy.deepcopy(payload)))
        event("agent.operation_queued", agent_operation_id=job.id, kind=kind)
        return job.public()

    @observed_operation("agent.operation")
    async def _run(self, job, payload):
        event("agent.operation_started", agent_operation_id=job.id, kind=job.kind)
        reconcile = (
            "Check Task List and the booking before retrying"
            if job.kind == "approve_task"
            else "Check My Bookings before retrying"
        )

        async def progress(stage):
            # Never put an MFA challenge, account identity, or credential into status.
            job.status, job.stage = "running", stage
            job.updated_at = self.clock()

        async def notify(message):
            job.status, job.stage = (
                "awaiting_mfa",
                "Check your private Telegram chat and approve this sign-in in Authenticator",
            )
            job.updated_at = self.clock()
            if self.notify is None:
                raise AuthenticationRequired(
                    "Configure private Telegram MFA notifications or use sagasu login first."
                )
            await self.notify(message)

        try:
            async with self.queue:
                job.status, job.stage = "running", "Opening FBS"
                result = await self.backend.run(job.kind, payload, progress, notify)
                result = copy.deepcopy(result)
                receipt = result.pop("receipt_png", None)
                if receipt is not None:
                    if isinstance(receipt, bytes) and len(receipt) <= 4 * 1024 * 1024:
                        job.receipt = receipt
                    else:
                        result["receipt_error"] = (
                            "Screenshot exceeds the MCP receipt size limit."
                        )
                job.result, job.status, job.stage = result, "succeeded", "Complete"
        except asyncio.CancelledError:
            job.status = (
                "outcome_unknown"
                if job.kind in WRITE_KINDS and job.status != "queued"
                else "cancelled"
            )
            job.stage = (
                "Stopped; " + reconcile
                if job.status == "outcome_unknown"
                else "Stopped"
            )
        except Exception as error:
            unknown = isinstance(
                error,
                (
                    BookingOutcomeUnknown,
                    CancellationOutcomeUnknown,
                    TaskApprovalOutcomeUnknown,
                ),
            ) or (
                job.kind in WRITE_KINDS
                and not isinstance(
                    error, (AuthenticationRequired, BookingUnavailable, TaskUnavailable)
                )
            )
            job.status = "outcome_unknown" if unknown else "failed"
            job.stage = reconcile if unknown else "Operation failed"
            job.error = {
                "code": type(error).__name__,
                "message": job.stage
                + ". Run sagasu doctor and inspect the redacted server logs.",
            }
            event(
                "agent.operation_failed",
                level=40,
                agent_operation_id=job.id,
                error_type=type(error).__name__,
                outcome=job.status,
            )
        finally:
            job.updated_at = self.clock()
            event(
                "agent.operation_finished",
                agent_operation_id=job.id,
                outcome=job.status,
            )

    def result(self, operation_id, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 50:
            raise AgentError("Use offset >= 0 and limit between 1 and 50.")
        job = self.get(operation_id)
        output = job.public()
        if job.result is not None:
            result = copy.deepcopy(job.result)
            for key in ("rooms", "bookings", "tasks"):
                if isinstance(result.get(key), list):
                    rows = result[key]
                    result[key] = rows[offset : offset + limit]
                    result["pagination"] = {
                        "offset": offset,
                        "limit": limit,
                        "returned_total": len(rows),
                        "next_offset": (
                            offset + limit if offset + limit < len(rows) else None
                        ),
                    }
            output["result"] = result
        return output

    def search(self, config=None):
        try:
            if config is None:
                search = search_config_from_bot(
                    self.profile.preferences() if self.profile else {}
                )
            else:
                search = SearchConfig.from_mapping(config)
            require_future_booking(search)
        except (ValueError, KeyError, TypeError, BookingUnavailable):
            raise AgentError(
                "Use a future SGT date (DD-Mon-YYYY) and HH:MM times on half-hour boundaries. Omit config for the next two hours."
            ) from None
        return self.start("search", search)

    def _completed(self, operation_id, kind):
        job = self.get(operation_id)
        if job.kind != kind or job.status != "succeeded":
            raise AgentError(f"A successful {kind} operation is required first.")
        return job.result

    def review_booking(
        self, search_operation_id, room, purpose, co_booker=None, cobooker_id=None
    ):
        if cobooker_id is not None:
            if co_booker is not None:
                raise AgentError(
                    "Provide either a saved co-booker ID or an email/name, not both."
                )
            co_booker = self.personal_profile().resolve_contact(cobooker_id)
        elif co_booker and "@" not in co_booker:
            co_booker = self.personal_profile().resolve_contact(co_booker)
        if not co_booker:
            raise AgentError(
                "Provide a co-booker email or choose a saved co-booker ID."
            )
        result = self._completed(search_operation_id, "search")
        search = SearchConfig.from_mapping(result["config"])
        require_future_booking(search)
        from .fbs_client import RoomAvailability, TimeSlot

        match = [row for row in result["rooms"] if row["name"] == room]
        if len(match) != 1 or not RoomAvailability(
            room, [TimeSlot(**slot) for slot in match[0]["timeslots"]]
        ).is_free(search.start_time, search.end_time):
            raise AgentError(
                "Choose a room free for the entire window in this search result."
            )
        if not 1 <= len(purpose.strip()) <= 200 or not SMU_EMAIL_RE.fullmatch(
            co_booker.strip()
        ):
            raise AgentError(
                "Provide a purpose of 1–200 characters and a valid SMU co-booker email."
            )
        request = BookingRequest(
            search, room, purpose.strip(), co_booker.strip().lower()
        )
        return self._review("book", request, request.to_dict())

    def review_cancellation(self, bookings_operation_id, reference_number, reason):
        result = self._completed(bookings_operation_id, "bookings")
        rows = [
            row
            for row in result["bookings"]
            if row["reference_number"] == reference_number
        ]
        if len(rows) != 1 or not rows[0].get("can_cancel"):
            raise AgentError(
                "Choose a uniquely listed booking that FBS allows you to cancel."
            )
        if not 1 <= len(reason.strip()) <= 200:
            raise AgentError("Provide a cancellation reason of 1–200 characters.")
        expected = copy.deepcopy(rows[0])
        return self._review(
            "cancel",
            (expected, reason.strip()),
            {"booking": expected, "reason": reason.strip()},
        )

    def _task(self, tasks_operation_id, reference_number):
        result = self._completed(tasks_operation_id, "tasks")
        rows = [
            task
            for task in result["tasks"]
            if task["reference_number"] == reference_number
        ]
        if len(rows) != 1:
            raise AgentError(
                "Choose one uniquely listed task reference from a successful Task List operation."
            )
        return copy.deepcopy(rows[0])

    def review_task_approval(self, tasks_operation_id, reference_number):
        task = self._task(tasks_operation_id, reference_number)
        if not task.get("can_approve"):
            raise AgentError(
                "FBS does not expose approval for this task. Refresh Task List."
            )
        return self._review(
            "approve_task",
            task,
            {
                "task": task,
                "action": "Approve this task only; final booking status may still be pending",
            },
        )

    def task_details(self, tasks_operation_id, reference_number):
        task = self._task(tasks_operation_id, reference_number)
        if not task.get("can_view"):
            raise AgentError("FBS does not expose a unique VIEW action for this task.")
        return self.start("task_details", task)

    def _review(self, kind, payload, summary):
        self.prune()
        if len(self.reviews) >= self.limit:
            raise AgentError(
                "Too many pending reviews; wait for expiry or discard one."
            )
        review = {
            "review_id": secrets.token_urlsafe(16),
            "kind": kind,
            "summary": summary,
            "expires_at": self.clock() + 600,
            "payload": payload,
        }
        self.reviews[review["review_id"]] = review
        return self.review(review["review_id"])

    def review(self, review_id):
        self.prune()
        if review_id not in self.reviews:
            raise AgentError(
                "Review expired, consumed, or unknown. Prepare a new review."
            )
        return copy.deepcopy(
            {
                key: value
                for key, value in self.reviews[review_id].items()
                if key != "payload"
            }
        )

    def validate(self, review_id):
        self.review(review_id)
        review = self.reviews[review_id]
        if review["kind"] != "book":
            raise AgentError(
                "Only booking reviews support validation without submission."
            )
        require_future_booking(review["payload"].search)
        return self.start("validate", review["payload"])

    def confirm(self, review_id):
        self.review(review_id)
        review = self.reviews[review_id]
        if review["kind"] == "book":
            require_future_booking(review["payload"].search)
        job = self.start(review["kind"], review["payload"])
        del self.reviews[
            review_id
        ]  # Consume before yielding; concurrent/replayed confirmations fail.
        return job

    def discard(self, review_id):
        self.review(review_id)
        del self.reviews[review_id]
        return {"discarded": True}

    async def cancel(self, operation_id):
        job = self.get(operation_id)
        if not job.terminal:
            if job.kind in WRITE_KINDS and job.status != "queued":
                raise AgentError(
                    "A write is running. Wait for verification; it cannot safely be interrupted."
                )
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            if (
                not job.terminal
            ):  # Task cancelled before its first coroutine instruction.
                job.status, job.stage, job.updated_at = (
                    "cancelled",
                    "Stopped",
                    self.clock(),
                )
        return job.public()

    async def close(self):
        self.closed = True
        active = [job for job in self.jobs.values() if not job.terminal]
        for job in active:
            if job.kind not in WRITE_KINDS or job.status == "queued":
                job.task.cancel()
        if active:
            _, pending = await asyncio.wait([job.task for job in active], timeout=90)
            for task in pending:
                task.cancel()
            await asyncio.gather(*(job.task for job in active), return_exceptions=True)
            for job in active:
                if not job.terminal:
                    job.status, job.stage, job.updated_at = (
                        "cancelled",
                        "Stopped",
                        self.clock(),
                    )
