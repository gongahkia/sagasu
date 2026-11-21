"""Bounded read-only watches for FBS's exact Pending Approval status."""

import asyncio
from time import monotonic
from asyncio import sleep

from .config import ConfigurationRequired
from .fbs_client import AuthenticationRequired
from .observability import step
from .operation_diagnostics import diagnostic_scope, MAX_EVENTS


TARGET = "pending approval"


def observation(listing, reference):
    if listing.get("complete") is not True:
        return None, "incomplete_listing"
    rows = [
        row
        for row in listing.get("bookings", [])
        if row.get("reference_number") == reference
    ]
    if len(rows) != 1:
        return None, "reference_missing_or_ambiguous"
    status = rows[0].get("fbs_status")
    if not isinstance(status, str) or not status.strip():
        return None, "status_unavailable"
    return status, None


async def run_watch(service, job, payload):
    # Sleep outside the shared browser queue so other reads and writes can proceed.
    deadline = monotonic() + payload["timeout_seconds"]
    result = {
        "reference_number": payload["reference_number"],
        "target_status": "Pending Approval",
        "target_status_observed": False,
        "last_observed_status": None,
        "last_checked_at": None,
        "checks": 0,
        "last_read_error": None,
        "outcome": "watching",
        "expires_at": service.clock() + payload["timeout_seconds"],
        "delivery": "MCP result only; poll wait_operation or operation_result",
        "survives_host_restart": False,
    }
    job.result = result

    async def progress(stage):
        job.status, job.stage = "running", stage
        job.auth_prompt = None
        job.auth_event.clear()
        job.updated_at = service.clock()

    async def notify(prompt):
        job.status = "awaiting_mfa"
        job.stage = "Read authentication_status and approve Microsoft sign-in"
        job.auth_prompt = prompt
        job.auth_event.set()
        job.updated_at = service.clock()

    async def read():
        check = result["checks"]
        with diagnostic_scope() as diagnostic:
            try:
                with step("watch.queue"):
                    await service.queue.acquire()
                try:
                    with step("watch.read"):
                        return await service.backend.run(
                            "bookings", None, progress, notify
                        )
                finally:
                    service.queue.release()
            finally:
                # Retain recent checks and the latest failure independently: a later
                # successful retry must not erase the evidence needed for debugging.
                events = diagnostic.events
                errors = [item for item in events if item.get("error_type")]
                summary = {
                    "check": check,
                    "error_type": errors[-1]["error_type"] if errors else None,
                    "screenshot_status": diagnostic.screenshot_status,
                }
                job.diagnostic_checks = (job.diagnostic_checks + [summary])[-20:]
                if errors:
                    job.diagnostic_last_failure = {
                        **summary,
                        "events": list(events),
                        "dropped_events": diagnostic.dropped,
                    }
                combined = job.diagnostic_events + events
                job.diagnostic_dropped += diagnostic.dropped + max(
                    0, len(combined) - MAX_EVENTS
                )
                job.diagnostic_events = combined[-MAX_EVENTS:]
                job.diagnostic_capture_status = diagnostic.screenshot_status
                if diagnostic.screenshot is not None and job.failure_screenshot is None:
                    job.failure_screenshot = {**diagnostic.screenshot, "check": check}
                if job.failure_screenshot is not None:
                    job.diagnostic_capture_status = "captured"

    try:
        await progress("Waiting for Pending Approval")
        while monotonic() < deadline:
            result["checks"] += 1
            try:
                listing = await asyncio.wait_for(read(), deadline - monotonic())
                status, error = observation(listing, payload["reference_number"])
                result["last_checked_at"] = service.clock()
                result["last_read_error"] = error
                if status is not None:
                    result["last_observed_status"] = status
                    normalized = " ".join(status.split()).casefold()
                    if normalized == TARGET:
                        result.update(
                            target_status_observed=True,
                            outcome="pending_approval_observed",
                        )
                        job.status, job.stage = (
                            "succeeded",
                            "Pending Approval observed; notify the user",
                        )
                        return
            except asyncio.TimeoutError:
                if monotonic() >= deadline:
                    break
                result["last_read_error"] = "TimeoutError"
            except (ConfigurationRequired, AuthenticationRequired) as error:
                result["last_read_error"] = type(error).__name__
                result["outcome"] = "blocked"
                job.status, job.stage = (
                    "failed",
                    "Watch blocked; target status not observed",
                )
                job.error = {
                    "code": type(error).__name__,
                    "message": job.stage,
                    "retry_safe": True,
                    "next_tool": (
                        "configuration_status"
                        if isinstance(error, ConfigurationRequired)
                        else "session_status"
                    ),
                }
                return
            except Exception as error:
                # Keep private page text, identities and authentication prompts out of results.
                result["last_read_error"] = type(error).__name__
            await progress("Waiting for Pending Approval")
            await sleep(
                min(payload["interval_seconds"], max(0, deadline - monotonic()))
            )
        result["outcome"] = "timed_out"
        job.status, job.stage = (
            "succeeded",
            "Watch deadline reached; target status not observed",
        )
    except asyncio.CancelledError:
        result["outcome"] = "cancelled"
        job.status, job.stage = "cancelled", "Watch stopped"
    except Exception:
        result["outcome"] = "failed"
        job.status, job.stage = "failed", "Watch failed; target status not observed"
        job.error = {
            "code": "BookingWatchFailed",
            "message": job.stage,
            "retry_safe": True,
        }
    finally:
        job.auth_prompt = None
        job.auth_event.clear()
        job.updated_at = service.clock()
