"""Repeatable live read measurements over MCP; exported reports contain no records.

The CLI owns the browser profile exclusively. Stop the shared host before running.
Browser contexts close before each case's first sample except current-page
screenshots; repeated samples can reuse them. MCP is in-process: timings exclude
agent reasoning and stdio startup.
"""

import asyncio
import fnmatch
import json
import logging
import os
import platform
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .action_journal import ActionJournal
from .config import ConfigError
from .fbs_client import (
    FBS_TIMEZONE,
    close_shared_browser,
    RoomAvailability,
    TimeSlot,
    SearchConfig,
    require_future_booking,
    BookingUnavailable,
)
from .mcp_service import AgentService, Backend
from .personal import PersonalStore
from .personal_options import SCRAPE_PARAM_CHOICES
from .runtime import InstanceLock

LIVE_TOOLS = frozenset(
    {
        "server_status",
        "configuration_status",
        "search_options",
        "get_search_preferences",
        "get_room_preferences",
        "list_cobookers",
        "list_booking_templates",
        "action_history",
        "session_status",
        "browser_screenshot",
        "search_rooms",
        "plan_rooms",
        "list_bookings",
        "list_tasks",
        "booking_details",
        "room_details",
        "credit_status",
        "booking_options",
        "amendment_options",
        "check_booking_clashes",
        "list_saved_plans",
        "refresh_saved_plan",
        "task_details",
        "search_cobookers",
        "available_rooms",
        "search_alternatives",
        "repeat_bookings",
        "wait_operation",
        "operation_result",
        "operation_status",
        "cancel_operation",
    }
)
BUILDINGS = {
    "scis1": "School of Computing & Information Systems 1",
    "scis2": "School of Economics/School of Computing & Information Systems 2",
    "soa": "School of Accountancy",
    "library": "Li Ka Shing Library",
}


def summarize(samples):
    successful = [s["elapsed_ms"] for s in samples if s["status"] == "success"]
    return {
        "samples": len(samples),
        "successes": len(successful),
        "failures": sum(s["status"] == "failed" for s in samples),
        "first_ms": samples[0]["elapsed_ms"] if samples else None,
        "median_ms": round(statistics.median(successful), 3) if successful else None,
        "min_ms": min(successful) if successful else None,
        "max_ms": max(successful) if successful else None,
        "warm_median_ms": (
            round(
                statistics.median(
                    [s["elapsed_ms"] for s in samples[1:] if s["status"] == "success"]
                ),
                3,
            )
            if any(s["status"] == "success" for s in samples[1:])
            else None
        ),
        "p95_ms": (
            round(statistics.quantiles(successful, n=100, method="inclusive")[94], 3)
            if len(successful) >= 20
            else None
        ),
    }


class PhaseCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.steps = defaultdict(list)
        self.browser_reused = []

    def emit(self, record):
        fields = getattr(record, "fields", {})
        if getattr(record, "event", None) == "step.completed":
            self.steps[record.step].append(fields["duration_ms"])
        elif getattr(record, "event", None) == "browser.ready":
            self.browser_reused.append(fields.get("reused", False))

    def reset(self):
        self.steps.clear()
        self.browser_reused.clear()


class BenchmarkFailure(Exception):
    def __init__(self, code):
        self.code = code


def snapshot_database(source, destination):
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(destination)) as snapshot,
    ):
        original.backup(snapshot)
    destination.chmod(0o600)


class Harness:
    def __init__(
        self,
        client,
        service,
        capture,
        repetitions,
        progress=None,
        selected=None,
        checkpoint=None,
    ):
        self.client, self.service, self.capture = client, service, capture
        self.repetitions, self.progress = repetitions, progress
        self.cases = []
        self.latest = {}
        self.selected = selected or []
        self.checkpoint = checkpoint
        self.room_sets = {}
        self.calls = self.structured_bytes = 0

    def includes(self, name):
        return not self.selected or any(
            fnmatch.fnmatchcase(name, pattern) for pattern in self.selected
        )

    async def call(self, name, arguments=None):
        if name not in LIVE_TOOLS:
            raise BenchmarkFailure("benchmark_write_forbidden")
        response = await self.client.call_tool(name, arguments or {})
        self.calls += 1
        self.structured_bytes += len(
            json.dumps(
                response.structured_content, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        if response.is_error:
            raise BenchmarkFailure("tool_rejected")
        return response.structured_content

    async def job(self, name, arguments=None):
        initial = await self.call(name, arguments)
        operation_id = initial["operation_id"]
        try:
            while True:
                status = await self.call(
                    "wait_operation",
                    {"operation_id": operation_id, "timeout": 20, "limit": 50},
                )
                if status["status"] == "awaiting_mfa":
                    raise BenchmarkFailure("authentication_required")
                if status["terminal"]:
                    if status["status"] != "succeeded":
                        error = status.get("error") or {}
                        raise BenchmarkFailure(
                            error.get("reason")
                            or error.get("code")
                            or "operation_failed"
                        )
                    result = status["result"]
                    # dependencies need the full snapshot, while timed tool results stay paginated.
                    self.latest[name] = (
                        operation_id,
                        self.service.get(operation_id).result,
                    )
                    if result.get("partial_failure") or any(
                        p.get("status") == "failed" for p in result.get("probes", [])
                    ):
                        raise BenchmarkFailure("partial_failure")
                    return result
        finally:
            if not self.service.get(operation_id).terminal:
                await self.service.cancel(operation_id)

    async def measure(
        self, name, operation, *, repetitions=None, preserve_browser=False
    ):
        if not self.includes(name):
            return None
        if not preserve_browser:
            await close_shared_browser()
        samples = []
        case = {"name": name, "in_progress": True, "measurements": samples}
        self.cases.append(case)
        for index in range(repetitions or self.repetitions):
            self.capture.reset()
            self.calls = self.structured_bytes = 0
            started = time.perf_counter_ns()
            sample = {"status": "success"}
            try:
                async with asyncio.timeout(180):
                    result = await operation()
                if isinstance(result, dict):
                    if (
                        isinstance(result.get("rooms"), list)
                        and "search_rooms" in self.latest
                    ):
                        self.room_sets[name] = {
                            r["name"] for r in self.latest["search_rooms"][1]["rooms"]
                        }
                    sample["counts"] = {
                        key: len(result[key])
                        for key in [
                            "rooms",
                            "bookings",
                            "tasks",
                            "candidates",
                            "slots",
                            "probes",
                        ]
                        if isinstance(result.get(key), list)
                    }
                    if "complete" in result:
                        sample["complete"] = result["complete"]
                    if result.get("read_backend"):
                        sample["backend"] = result["read_backend"]["name"]
                        sample["fallback_reason"] = result["read_backend"].get(
                            "fallback_reason"
                        )
            except Exception as error:
                sample = {
                    "status": "failed",
                    "error_code": getattr(error, "code", None) or type(error).__name__,
                }
            sample["elapsed_ms"] = round((time.perf_counter_ns() - started) / 1e6, 3)
            sample["mcp_calls"] = self.calls
            sample["structured_response_bytes"] = self.structured_bytes
            sample["phases_ms"] = {
                key: round(sum(values), 3) for key, values in self.capture.steps.items()
            }
            sample["browser_reused"] = list(self.capture.browser_reused)
            samples.append(sample)
            case.update(summarize(samples))
            if self.checkpoint:
                self.checkpoint(self.cases)
            if self.progress:
                self.progress(
                    f"{name} {index + 1}/{repetitions or self.repetitions}: {sample['status']} {sample['elapsed_ms']:.0f}ms"
                    + (f" ({sample['error_code']})" if sample.get("error_code") else "")
                )
            if sample.get("error_code") in {
                "authentication_required",
                "session_expired",
                "AuthenticationRequired",
            }:
                break
        case["in_progress"] = False
        return case

    def skip(self, name, reason):
        if self.includes(name):
            self.cases.append({"name": name, "skipped": reason})


async def run_benchmark(
    settings,
    *,
    repetitions=5,
    mode="auto",
    date=None,
    start="10:00",
    end="11:00",
    cobooker_query=None,
    equipment_matrix=False,
    equipment_facility_types=None,
    timeout=3600,
    progress=None,
    selected=None,
    output=None,
):
    from mcp import Client
    from .mcp_server import create_server

    if (
        type(repetitions) is not int
        or not 1 <= repetitions <= 30
        or mode not in {"auto", "api", "browser"}
    ):
        raise ConfigError("Use 1–30 repetitions and backend auto, api or browser.")
    date = date or (datetime.now(FBS_TIMEZONE).date() + timedelta(days=1)).isoformat()
    try:
        require_future_booking(
            SearchConfig(
                datetime.strptime(date, "%Y-%m-%d").strftime("%d-%b-%Y"), start, end
            )
        )
    except (ValueError, TypeError, BookingUnavailable):
        raise ConfigError(
            "Choose a future ISO date and half-hour start/end times."
        ) from None
    config = {
        "date": date,
        "start_time": start,
        "end_time": end,
        "buildings": [BUILDINGS["scis1"]],
        "facility_types": ["Group Study Room"],
    }
    equipment_types = equipment_facility_types or config["facility_types"]
    if any(t not in SCRAPE_PARAM_CHOICES["facility_type"] for t in equipment_types):
        raise ConfigError("Choose advertised facility types for equipment checks.")
    report = {
        "version": __version__,
        "python": platform.python_version(),
        "started_at": datetime.now(FBS_TIMEZONE).isoformat(),
        "backend_mode": mode,
        "comparison_context": {
            "method": "in_process_mcp_v1",
            "backend_mode": mode,
            "python_minor": ".".join(platform.python_version_tuple()[:2]),
            "platform": platform.system(),
            "search_config": config,
            "equipment_facility_types": equipment_types,
        },
        "repetitions": repetitions,
        "live": True,
        "read_only": True,
        "measurement_scope": "In-process MCP tool plus job completion; excludes agent reasoning and stdio startup. Dependency setup is excluded from individual case timing.",
        "first_sample": "Shared Chromium context closed before each case except current-page screenshots; subsequent samples may reuse it. HTTP reads may start their own request contexts.",
        "p95_policy": "Only reported with at least 20 successful samples.",
        "phase_policy": "Nested phase totals are inclusive; do not sum them as exclusive time.",
        "response_size_policy": "Compact UTF-8 structured JSON summed over MCP responses, including polls; excludes text/image blocks and transport framing. This is not a token count.",
        "latency_policy": "Median/min/max/warm/p95 use successful samples; failures are retained individually. first_ms is the first attempt.",
        "live_writes": "Booking submission, cancellation, approval and profile mutations are excluded; benchmark those with synthetic fixtures.",
        "cases": [],
        "selected_cases": selected or ["*"],
        "in_progress": True,
        "system_load_at_start": (
            list(os.getloadavg()) if hasattr(os, "getloadavg") else None
        ),
    }
    settings.activate()
    capture = PhaseCapture()
    logger = logging.getLogger("sagasu.observability")
    old_level = logger.level
    old_propagate = logger.propagate
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    service = None
    try:
        with (
            InstanceLock(settings.profile),
            tempfile.TemporaryDirectory(prefix="sagasu-benchmark-") as temporary,
        ):
            prefs = Path(temporary) / "preferences.sqlite3"
            if settings.preferences.exists():
                snapshot_database(settings.preferences, prefs)
            history = Path(temporary) / "actions.sqlite3"
            if settings.journal.exists():
                snapshot_database(settings.journal, history)
            backend = Backend(interactive_auth=False)
            backend.reads.mode = mode
            service = AgentService(
                backend=backend,
                profile=PersonalStore(prefs),
                journal=ActionJournal(history),
            )
            async with (
                asyncio.timeout(timeout),
                Client(create_server(service)) as client,
            ):

                def checkpoint(cases):
                    report["cases"] = cases
                    if output:
                        save_report(output, report)

                harness = Harness(
                    client,
                    service,
                    capture,
                    repetitions,
                    progress,
                    selected,
                    checkpoint,
                )
                try:
                    await harness.job("session_status")
                except BenchmarkFailure:
                    raise ConfigError(
                        "Saved FBS session is unavailable. Run sagasu login separately before benchmarking."
                    ) from None
                for tool in [
                    "server_status",
                    "configuration_status",
                    "search_options",
                    "get_search_preferences",
                    "get_room_preferences",
                    "list_cobookers",
                    "list_booking_templates",
                    "list_saved_plans",
                    "action_history",
                ]:
                    await harness.measure(
                        tool,
                        lambda tool=tool: harness.call(tool),
                        repetitions=max(repetitions, 20),
                    )
                await harness.measure(
                    "session_status", lambda: harness.job("session_status")
                )
                await harness.measure(
                    "credit_status", lambda: harness.job("credit_status")
                )
                await harness.measure(
                    "check_booking_clashes",
                    lambda: harness.job(
                        "check_booking_clashes",
                        {
                            "windows": [
                                {
                                    key: config[key]
                                    for key in ("date", "start_time", "end_time")
                                }
                            ]
                        },
                    ),
                )
                for label, building in BUILDINGS.items():
                    query = {
                        **config,
                        "buildings": [building],
                        "facility_types": ["Group Study Room", "Project Room"],
                    }
                    await harness.measure(
                        f"search_{label}",
                        lambda query=query: harness.job(
                            "search_rooms", {"config": query}
                        ),
                    )
                for name, patch in [
                    ("search_full_day", {"start_time": "08:00", "end_time": "23:30"}),
                    ("search_floor", {"floors": ["Level 2"]}),
                    ("search_capacity", {"capacity": "From6To10Pax"}),
                    ("search_projector", {"equipment": ["Projector"]}),
                    ("search_tv", {"equipment": ["TV Panel"]}),
                    ("search_projector_tv", {"equipment": ["Projector", "TV Panel"]}),
                ]:
                    query = {**config, **patch}
                    if "equipment" in patch:
                        query["facility_types"] = equipment_types
                    await harness.measure(
                        name,
                        lambda query=query: harness.job(
                            "search_rooms", {"config": query}
                        ),
                    )
                await harness.measure(
                    "plan_flexible",
                    lambda: harness.job(
                        "plan_rooms",
                        {
                            "config": {
                                **config,
                                "start_time": "12:00",
                                "end_time": "18:00",
                            },
                            "duration_minutes": 120,
                        },
                    ),
                )
                await harness.measure(
                    "plan_common_dates",
                    lambda: harness.job(
                        "plan_rooms",
                        {
                            "config": {
                                **config,
                                "start_time": "12:00",
                                "end_time": "18:00",
                            },
                            "duration_minutes": 60,
                            "dates": [
                                date,
                                (
                                    datetime.strptime(date, "%Y-%m-%d").date()
                                    + timedelta(days=1)
                                ).isoformat(),
                            ],
                            "mode": "common",
                        },
                    ),
                )
                trio = [
                    harness.room_sets.get(name)
                    for name in ["search_projector", "search_tv", "search_projector_tv"]
                ]
                if all(value is not None for value in trio):
                    projector, tv, combined = trio
                    intersection, union = projector & tv, projector | tv
                    report["equipment_matching"] = {
                        "scope": "SCIS1 for this run's date/window",
                        "facility_types": equipment_types,
                        "projector_rooms": len(projector),
                        "tv_rooms": len(tv),
                        "combined_rooms": len(combined),
                        "matches_intersection": combined == intersection,
                        "matches_union": combined == union,
                        "discriminating": intersection != union,
                    }
                if equipment_matrix:
                    for label, building in BUILDINGS.items():
                        for equipment in SCRAPE_PARAM_CHOICES["equipment"]:
                            query = {
                                **config,
                                "buildings": [building],
                                "facility_types": equipment_facility_types
                                or ["Group Study Room", "Project Room"],
                                "equipment": [equipment],
                            }
                            await harness.measure(
                                f"equipment/{label}/{equipment}",
                                lambda query=query: harness.job(
                                    "search_rooms", {"config": query}
                                ),
                                repetitions=1,
                            )
                await harness.measure(
                    "list_bookings", lambda: harness.job("list_bookings")
                )
                bookings = harness.latest.get("list_bookings", (None, {}))[1].get(
                    "bookings", []
                )
                if not bookings:
                    try:
                        await harness.job("list_bookings")
                        bookings = harness.latest["list_bookings"][1].get(
                            "bookings", []
                        )
                    except BenchmarkFailure:
                        pass
                for label, rows in [
                    (
                        "confirmed",
                        [
                            r
                            for r in bookings
                            if r["fbs_status"].casefold() == "confirmed"
                        ],
                    ),
                    (
                        "pending",
                        [
                            r
                            for r in bookings
                            if "pending" in r["fbs_status"].casefold()
                        ],
                    ),
                ]:
                    if rows:
                        ref = rows[-1]["reference_number"]
                        for extra in [False, True]:
                            await harness.measure(
                                f"booking_details_{label}_{'full' if extra else 'basic'}",
                                lambda ref=ref, extra=extra: harness.job(
                                    "booking_details",
                                    {
                                        "reference_number": ref,
                                        "source": "bookings",
                                        "include_additional": extra,
                                    },
                                ),
                            )
                    else:
                        harness.skip(
                            f"booking_details_{label}",
                            "No matching booking in the current account.",
                        )
                await harness.measure("list_tasks", lambda: harness.job("list_tasks"))
                if harness.includes("browser_screenshot"):
                    # API reads do not necessarily leave a loaded browser page.
                    await harness.job("session_status")
                    await harness.measure(
                        "browser_screenshot",
                        lambda: harness.job("browser_screenshot"),
                        preserve_browser=True,
                    )
                tasks_id, task_result = harness.latest.get("list_tasks", (None, {}))
                if tasks_id is None and harness.includes("task_details"):
                    try:
                        await harness.job("list_tasks")
                        tasks_id, task_result = harness.latest["list_tasks"]
                    except BenchmarkFailure:
                        pass
                tasks = [r for r in task_result.get("tasks", []) if r.get("can_view")]
                if tasks:
                    await harness.measure(
                        "task_details",
                        lambda: harness.job(
                            "task_details",
                            {
                                "tasks_operation_id": tasks_id,
                                "reference_number": tasks[0]["reference_number"],
                            },
                        ),
                    )
                else:
                    harness.skip(
                        "task_details",
                        (
                            "Task listing failed; available tasks are unknown."
                            if tasks_id is None
                            else "No task with a permitted VIEW action."
                        ),
                    )
                await harness.job("search_rooms", {"config": config})
                source_id, source = harness.latest["search_rooms"]
                await harness.measure(
                    "available_rooms",
                    lambda: harness.call(
                        "available_rooms", {"operation_id": source_id}
                    ),
                    repetitions=max(repetitions, 20),
                )
                await harness.measure(
                    "search_alternatives",
                    lambda: harness.job(
                        "search_alternatives",
                        {"operation_id": source_id, "shift_minutes": 30},
                    ),
                )
                free = [
                    r
                    for r in source["rooms"]
                    if RoomAvailability(
                        r["name"], [TimeSlot(**s) for s in r["timeslots"]]
                    ).is_free(start, end)
                ]
                if source["rooms"]:
                    await harness.measure(
                        "room_details",
                        lambda: harness.job(
                            "room_details", {"room": source["rooms"][0]["name"]}
                        ),
                    )
                else:
                    harness.skip(
                        "room_details",
                        "No room identity was returned by the source search.",
                    )
                if cobooker_query and free:
                    await harness.measure(
                        "directory_partial",
                        lambda: harness.job(
                            "search_cobookers",
                            {
                                "query": cobooker_query,
                                "search_operation_id": source_id,
                                "room": free[0]["name"],
                            },
                        ),
                    )
                else:
                    harness.skip(
                        "directory_partial",
                        "Provide --cobooker-query and a window with a free room.",
                    )
                if cobooker_query:
                    await harness.measure(
                        "directory_standalone",
                        lambda: harness.job(
                            "search_cobookers", {"query": cobooker_query}
                        ),
                    )
                else:
                    harness.skip("directory_standalone", "Provide --cobooker-query.")
                if free:
                    await harness.measure(
                        "booking_options",
                        lambda: harness.job(
                            "booking_options",
                            {"search_operation_id": source_id, "room": free[0]["name"]},
                        ),
                    )
                else:
                    harness.skip(
                        "booking_options",
                        "No free source room for an unsent booking form.",
                    )
                if bookings:
                    await harness.job("list_bookings")
                    book_id, _ = harness.latest["list_bookings"]
                    await harness.measure(
                        "repeat_plan",
                        lambda: harness.job(
                            "repeat_bookings",
                            {
                                "date": date,
                                "bookings_operation_id": book_id,
                                "reference_numbers": [bookings[-1]["reference_number"]],
                            },
                        ),
                    )
                else:
                    harness.skip(
                        "repeat_plan", "No booking available as a repeat source."
                    )
                report["cases"] = harness.cases
                report["ok"] = not any(case.get("failures") for case in harness.cases)
    except TimeoutError:
        report["ok"] = False
        report["error_code"] = "benchmark_timeout"
    finally:
        if service:
            await service.close()
        await close_shared_browser()
        logger.removeHandler(capture)
        logger.setLevel(old_level)
        logger.propagate = old_propagate
    report["finished_at"] = datetime.now(FBS_TIMEZONE).isoformat()
    report["in_progress"] = False
    report["system_load_at_end"] = (
        list(os.getloadavg()) if hasattr(os, "getloadavg") else None
    )
    if output:
        save_report(output, report)
    return report


def save_report(path, report):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, encoding="utf-8", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
