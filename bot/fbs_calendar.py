"""Complete read-only extraction of FBS's scroll-rendered DayPilot calendar."""

import asyncio
import logging
from .observability import event, observed_step
import math
from datetime import datetime, timedelta, timezone

try:
    from .fbs_client import (
        LayoutChanged,
        RoomAvailability,
        TimeSlot,
        add_free_windows,
        parse_event_title,
        minutes_to_time,
        time_to_minutes,
    )
except ImportError:
    if __package__:
        raise
    from fbs_client import (
        LayoutChanged,
        RoomAvailability,
        TimeSlot,
        add_free_windows,
        parse_event_title,
        minutes_to_time,
        time_to_minutes,
    )


# Only room names/counts and rendering configuration leave this evaluation.
METADATA = """() => {
    const scheduler = window.dps;
    if (!scheduler || !Array.isArray(scheduler.resources) ||
        !scheduler.events || !Array.isArray(scheduler.events.list)) return null;
    const names = [];
    const visit = rows => {
        for (const row of rows) {
            if (Array.isArray(row.children) && row.children.length) visit(row.children);
            else names.push(row.name);
        }
    };
    visit(scheduler.resources);
    return {names, event_count: scheduler.events.list.length,
        dynamic_loading: scheduler.dynamicLoading,
        event_rendering: scheduler.dynamicEventRendering};
}"""

# Capture coordinates atomically; header and event positions share content space.
ROWS = """scroll => {
    const root = scroll.closest('#dps');
    const origin = scroll.getBoundingClientRect().top;
    const rows = [...root.querySelectorAll('.scheduler_bluewhite_rowheader_inner')].map(el => {
        const row = el.closest('.scheduler_bluewhite_rowheader');
        const box = row.getBoundingClientRect();
        return {name: el.innerText.trim(), top: box.top - origin + scroll.scrollTop,
            bottom: box.bottom - origin + scroll.scrollTop, height: box.height};
    });
    return {rows, top: scroll.scrollTop, height: scroll.clientHeight,
        full_height: scroll.scrollHeight};
}"""

EVENTS = """scroll => {
    const origin = scroll.getBoundingClientRect().top;
    const names = new Map();
    const visit = rows => rows.forEach(row => {
        names.set(row.id, row.name);
        if (row.children) visit(row.children);
    });
    visit(window.dps.resources);
    return [...scroll.closest('#dps').querySelectorAll(
        'div.scheduler_bluewhite_event')].map(el => {
        const box = el.getBoundingClientRect();
        const title = el.getAttribute('title');
        // The rendered DayPilot event points to its full dated model. Tooltip
        // clock text alone cannot distinguish an empty interval from a closed day.
        const data = el.event && el.event.data;
        const matches = data ? window.dps.events.list.filter(e =>
            e.id === data.id && e.resource === data.resource &&
            String(e.start) === String(data.start) && String(e.end) === String(data.end)) : [];
        const interval = matches.length === 1 ? [String(data.start), String(data.end)] : [null, null];
        return {title, resource_name: data ? names.get(data.resource) : null,
            start_date: interval[0], end_date: interval[1],
            top: box.top - origin + scroll.scrollTop,
            bottom: box.bottom - origin + scroll.scrollTop, height: box.height};
    });
}"""


def calendar_rooms(names, config, rows, events, event_count):
    """Fail closed on missing rows/events or ambiguous geometric assignment."""
    if len(set(names)) != len(names) or set(rows) != set(names):
        raise LayoutChanged("Not all matching rooms have readable calendar rows")
    if len(events) != event_count:
        raise LayoutChanged("Not all calendar events have been rendered")
    blocked = {name: [] for name in names}
    for event in events:
        slot = parse_event_title(event.get("title") or "")
        if not slot or event["height"] <= 0:
            raise LayoutChanged("Calendar contains an unreadable availability event")
        center = (event["top"] + event["bottom"]) / 2
        matches = [
            name for name, bounds in rows.items() if bounds[0] <= center < bounds[1]
        ]
        if len(matches) != 1:
            raise LayoutChanged("Calendar event cannot be assigned to exactly one room")
        if "resource_name" in event and event["resource_name"] != matches[0]:
            raise LayoutChanged("Calendar model and rendered room disagree")
        if "start_date" in event or "end_date" in event:
            slot = dated_event_slot(event, slot.status, config)
        elif time_to_minutes(slot.end) <= time_to_minutes(slot.start):
            raise LayoutChanged(
                "Calendar midnight interval requires full date boundaries"
            )
        # Keep only interval/status fields, even in this internal result.
        if slot is not None:
            blocked[matches[0]].append(TimeSlot(slot.start, slot.end, slot.status))
    return [
        RoomAvailability(
            name, add_free_windows(blocked[name], config.start_time, config.end_time)
        )
        for name in names
    ]


def dated_event_slot(event, status, config):
    """Intersect full widget timestamps with the requested Singapore-local window."""
    try:
        start = datetime.fromisoformat(event["start_date"])
        end = datetime.fromisoformat(event["end_date"])
        local_zone = timezone(timedelta(hours=8))
        if start.tzinfo:
            start = start.astimezone(local_zone).replace(tzinfo=None)
        if end.tzinfo:
            end = end.astimezone(local_zone).replace(tzinfo=None)
        if end <= start:
            raise ValueError()
        day = datetime.strptime(config.date, "%d-%b-%Y")
        start_minute = max(
            time_to_minutes(config.start_time),
            math.floor((start - day).total_seconds() / 60),
        )
        end_minute = min(
            time_to_minutes(config.end_time),
            math.ceil((end - day).total_seconds() / 60),
        )
        if start_minute >= end_minute:
            return None
        return TimeSlot(
            minutes_to_time(start_minute), minutes_to_time(end_minute), status
        )
    except (ValueError, TypeError, KeyError):
        raise LayoutChanged(
            "Calendar event has missing or invalid full date boundaries"
        ) from None


@observed_step("calendar.extract")
async def read_complete_calendar(frame, names, config):
    stage = "resource_readiness"
    try:
        async with asyncio.timeout(90):
            # FBS can replace its scheduler after the availability link appears.
            # Wait for the loaded resource tree to match the complete discovery list.
            await frame.wait_for_function(
                """expected => {
                    const data = ("""
                + METADATA
                + """)();
                    return data && data.names.length === expected.length &&
                        new Set(data.names).size === expected.length &&
                        expected.every(name => data.names.includes(name));
                }""",
                arg=names,
                timeout=30000,
            )
            stage = "resource_metadata"
            event("calendar.phase", level=logging.DEBUG, phase=stage)
            metadata = await frame.evaluate(METADATA)
            # An unloaded server window or virtual event list cannot prove coverage.
            if (
                metadata["dynamic_loading"] is not False
                or metadata["event_rendering"] != "Disabled"
            ):
                raise LayoutChanged(
                    "Calendar event loading requires complete discovery"
                )
            scroll = frame.locator("#dps .scheduler_bluewhite_scrollable")
            if await scroll.count() != 1:
                raise LayoutChanged("Calendar scroll area is not uniquely identifiable")
            await scroll.evaluate("(el) => {el.scrollTop = 0; el.scrollLeft = 0;}")
            stage = "row_scan"
            event("calendar.phase", level=logging.DEBUG, phase=stage)
            rows = {}
            for _ in range(200):
                # Allow DayPilot's scroll handler to render the next row headers.
                await frame.wait_for_timeout(250)
                view = await scroll.evaluate(ROWS)
                if view["height"] <= 0:
                    raise LayoutChanged("Calendar scroll area is not readable")
                for row in view["rows"]:
                    if row["name"] not in names or row["height"] <= 0:
                        continue
                    bounds = (row["top"], row["bottom"])
                    previous = rows.get(row["name"])
                    if previous and any(
                        abs(a - b) > 1 for a, b in zip(previous, bounds)
                    ):
                        raise LayoutChanged(
                            "Calendar room positions changed during extraction"
                        )
                    rows[row["name"]] = bounds
                if view["top"] + view["height"] >= view["full_height"] - 1:
                    break
                target = min(
                    view["full_height"] - view["height"],
                    view["top"] + max(1, view["height"] * 0.75),
                )
                await scroll.evaluate("(el, top) => {el.scrollTop = top;}", target)
            else:
                raise LayoutChanged("Calendar row discovery exceeded its bounded scan")
            await scroll.evaluate("(el) => {el.scrollTop = 0;}")
            await frame.wait_for_timeout(250)
            stage = "event_scan"
            event("calendar.phase", level=logging.DEBUG, phase=stage)
            events = await scroll.evaluate(EVENTS)
            final_metadata = await frame.evaluate(METADATA)
            if final_metadata != metadata:
                raise LayoutChanged("Calendar changed during extraction")
            stage = "coverage_validation"
            event("calendar.phase", level=logging.DEBUG, phase=stage)
            event(
                "calendar.coverage",
                rooms=len(rows),
                events=len(events),
                expected_events=metadata["event_count"],
            )
            return calendar_rooms(names, config, rows, events, metadata["event_count"])
    except LayoutChanged:
        raise
    except Exception:
        error = LayoutChanged("Complete calendar could not be read reliably")
        error.stage = stage
        raise error from None
