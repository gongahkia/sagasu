"""Bounded, task-local diagnostics; no payloads, URLs or exception messages."""

import asyncio
import contextvars
import re
from contextlib import contextmanager
from dataclasses import dataclass, field


_active = contextvars.ContextVar("sagasu_diagnostics", default=None)
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,100}\Z")
MAX_EVENTS = 100
MAX_IMAGE_BYTES = 4 * 1024 * 1024


@dataclass
class Diagnostics:
    events: list = field(default_factory=list)
    dropped: int = 0
    screenshot: dict | None = None
    capturing: bool = False
    attempted: bool = False
    screenshot_status: str = "not_attempted"


@contextmanager
def diagnostic_scope():
    capture = Diagnostics()
    token = _active.set(capture)
    try:
        yield capture
    finally:
        _active.reset(token)


def identifier(value):
    return value if isinstance(value, str) and NAME.fullmatch(value) else None


def record_event(name, step, elapsed, fields):
    capture = _active.get()
    if capture is None or capture.capturing:
        return
    item = {"event": identifier(name), "step": identifier(step)}
    for key, value in (
        ("elapsed_ms", elapsed),
        ("duration_ms", fields.get("duration_ms")),
    ):
        if type(value) in (int, float) and 0 <= value < 1e12:
            item[key] = round(value, 3)
    if identifier(fields.get("error_type")):
        item["error_type"] = fields["error_type"]
    for key in ("page", "row_count", "reported_total", "records_read"):
        value = fields.get(key)
        if type(value) is int and 0 <= value <= 1000000:
            item[key] = value
    direction = fields.get("direction")
    if isinstance(direction, str) and direction in {"first", "next"}:
        item["direction"] = direction
    if isinstance(fields.get("error_chain"), list):
        item["error_chain"] = [
            value for value in fields["error_chain"][:5] if identifier(value)
        ]
    backend = fields.get("backend")
    if isinstance(backend, str) and backend in {"api", "browser"}:
        item["backend"] = backend
    if isinstance(fields.get("error_frames"), list):
        item["error_frames"] = [
            {
                "file": identifier(frame.get("file")),
                "function": identifier(frame.get("function")),
                "line": frame.get("line"),
            }
            for frame in fields["error_frames"][-8:]
            if isinstance(frame, dict) and type(frame.get("line")) is int
        ]
    if len(capture.events) == MAX_EVENTS:
        capture.events.pop(0)
        capture.dropped += 1
    capture.events.append(item)


async def capture_failure(client, error):
    capture = _active.get()
    if capture is None or capture.capturing or capture.attempted:
        return
    from .fbs_client import AuthenticationRequired
    from .browser_debug import ScreenshotUnavailable

    if isinstance(
        error, (asyncio.CancelledError, AuthenticationRequired, ScreenshotUnavailable)
    ):
        capture.screenshot_status = (
            "skipped_authentication_or_cancellation"
            if not isinstance(error, ScreenshotUnavailable)
            else "capture_unavailable"
        )
        return
    if getattr(client, "page", None) is None:
        capture.screenshot_status = "no_browser_page"
        return
    capture.capturing = True
    capture.attempted = True
    capture.screenshot_status = "capture_failed"
    try:
        from .browser_debug import capture_browser_screenshot

        result = await capture_browser_screenshot(client)
        png = result.get("receipt_png")
        if isinstance(png, bytes) and len(png) <= MAX_IMAGE_BYTES:
            capture.screenshot_status = "captured"
            capture.screenshot = {
                "receipt_png": png,
                "receipt_kind": "operation_failure",
                "captured_at": result["captured_at"],
                "form_fields_masked": True,
                "scope": "first_observed_browser_failure_before_cleanup",
            }
        else:
            capture.screenshot_status = (
                "image_too_large" if isinstance(png, bytes) else "image_unavailable"
            )
    except Exception:
        # diagnostics must not replace an operation's outcome or trigger a retry.
        pass
    finally:
        capture.capturing = False


def support_bundle(job, setup):
    """JSON-safe support data; images and user-visible booking text stay separate."""
    checks = setup.get("checks", [])
    image = job.failure_screenshot
    image_metadata = (
        {
            key: image[key]
            for key in ("captured_at", "scope", "form_fields_masked", "check")
            if key in image
        }
        if image
        else None
    )
    return {
        "format": "sagasu.support.v1",
        "operation": {
            "operation_id": job.id,
            "kind": job.kind,
            "status": job.status,
            "terminal": job.terminal,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "error_type": identifier((job.error or {}).get("code")),
            "recovered": job.recovered,
        },
        "setup": {
            "operational": setup.get("operational"),
            "checks": [
                {
                    "name": identifier(c.get("name")),
                    "status": c.get("status"),
                    "blocks_startup": c.get("blocks_startup") is True,
                }
                for c in checks
                if isinstance(c, dict)
            ],
        },
        "events": list(job.diagnostic_events),
        "dropped_events": job.diagnostic_dropped,
        "failure_screenshot_available": job.failure_screenshot is not None,
        "failure_image": image_metadata,
        "screenshot_status": getattr(job, "diagnostic_capture_status", "not_attempted"),
        "watch_checks": list(getattr(job, "diagnostic_checks", [])),
        "latest_failed_check": getattr(job, "diagnostic_last_failure", None),
        "next_action": "Inspect the last failed step and code locations. Retrieve operation_receipt(failure=true) if an image is available; browser_screenshot shows only the current page, not the historical failure. Do not replay a write to obtain diagnostics.",
        "coverage": "Events retained for this in-memory operation only; recovered writes have no historical trace or image.",
        "excluded": [
            "credentials",
            "cookies",
            "URLs",
            "configuration values and paths",
            "booking details",
            "contacts",
            "purpose",
            "MFA prompts",
            "screenshots",
        ],
        "outcome_note": "Diagnostics do not prove success, absence of availability or safe retry. Reconcile uncertain writes before a new submission.",
    }
