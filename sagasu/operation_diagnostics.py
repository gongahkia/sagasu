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
        return
    if getattr(client, "page", None) is None:
        return
    capture.capturing = True
    capture.attempted = True
    try:
        from .browser_debug import capture_browser_screenshot

        result = await capture_browser_screenshot(client)
        png = result.get("receipt_png")
        if isinstance(png, bytes) and len(png) <= MAX_IMAGE_BYTES:
            capture.screenshot = {
                "receipt_png": png,
                "receipt_kind": "operation_failure",
                "captured_at": result["captured_at"],
                "form_fields_masked": True,
                "scope": "first_observed_browser_failure_before_cleanup",
            }
    except Exception:
        # diagnostics must not replace an operation's outcome or trigger a retry.
        pass
    finally:
        capture.capturing = False


def support_bundle(job, setup):
    """JSON-safe support data; images and user-visible booking text stay separate."""
    checks = setup.get("checks", [])
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
