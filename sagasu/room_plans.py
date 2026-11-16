"""Bounded read-only planning over complete, explicit FBS availability snapshots."""

import heapq
from dataclasses import asdict, replace
from datetime import datetime, timedelta

from playwright.async_api import Error as PlaywrightError

from .alternatives import result_from_dict
from .errors import public_failure
from .fbs_client import (
    FBS_TIMEZONE,
    FBSError,
    LayoutChanged,
    SearchConfig,
    minutes_to_time,
    time_to_minutes,
    require_future_booking,
    _noop_progress,
)
from .repeat import normalize_date
from .room_preferences import merge_preferences

MAX_DATES = 31
MAX_CANDIDATES = 200
MAX_EVALUATED_CANDIDATES = 100_000


def recurrence_dates(value):
    if not isinstance(value, dict) or set(value) - {
        "start_date",
        "end_date",
        "weekdays",
        "interval_weeks",
    }:
        raise ValueError(
            "Use a bounded recurrence with start_date, end_date, weekdays and optional interval_weeks."
        )
    try:
        start, end = [
            datetime.strptime(normalize_date(value[key]), "%d-%b-%Y").date()
            for key in ("start_date", "end_date")
        ]
    except (KeyError, ValueError, TypeError):
        raise ValueError(
            "Recurrence requires explicit valid start_date and end_date."
        ) from None
    weekdays, interval = value.get("weekdays"), value.get("interval_weeks", 1)
    if (
        not isinstance(weekdays, list)
        or not weekdays
        or len(weekdays) > 7
        or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays)
        or len(set(weekdays)) != len(weekdays)
        or type(interval) is not int
        or not 1 <= interval <= 4
        or not 0 <= (end - start).days <= 366
    ):
        raise ValueError(
            "Choose unique weekdays 0–6 (Monday–Sunday), interval_weeks 1–4 and an inclusive range of at most 366 days."
        )
    anchor = start - timedelta(days=start.weekday())
    dates = [
        day.strftime("%d-%b-%Y")
        for offset in range((end - start).days + 1)
        if (day := start + timedelta(days=offset)).weekday() in weekdays
        and ((day - anchor).days // 7) % interval == 0
    ]
    if not 1 <= len(dates) <= MAX_DATES:
        raise ValueError(
            "The recurrence must generate 1–31 dates; shorten or revise the range."
        )
    return dates


def prepare_plan(
    config,
    dates=None,
    duration_minutes=60,
    mode="any",
    allow_split=False,
    transition_minutes=0,
    preferences=None,
    *,
    now=None,
    recurrence=None,
    scope_sources=None,
):
    config = SearchConfig.from_mapping(config)
    now = now or datetime.now(FBS_TIMEZONE)
    if (
        type(duration_minutes) is not int
        or duration_minutes < 30
        or duration_minutes % 30
    ):
        raise ValueError("Duration must be a positive number of half-hour minutes.")
    if mode not in {"any", "common"} or type(allow_split) is not bool:
        raise ValueError(
            "Choose any-date or common-room mode and a boolean split option."
        )
    if mode == "common" and allow_split:
        raise ValueError(
            "Common-room mode preserves a single room; split alternatives require any-date mode."
        )
    if (
        type(transition_minutes) is not int
        or not 0 <= transition_minutes <= 120
        or transition_minutes % 30
    ):
        raise ValueError(
            "Room-change gaps must be 0–120 minutes on half-hour boundaries."
        )
    if (
        time_to_minutes(config.end_time) - time_to_minutes(config.start_time)
        < duration_minutes
    ):
        raise ValueError("The requested duration exceeds the search window.")
    if dates is not None and recurrence is not None:
        raise ValueError("Supply explicit dates OR recurrence, not both.")
    dates = (
        recurrence_dates(recurrence)
        if recurrence is not None
        else [config.date] if dates is None else dates
    )
    if (
        not isinstance(dates, list)
        or not 1 <= len(dates) <= MAX_DATES
        or any(not isinstance(d, str) for d in dates)
    ):
        raise ValueError("Choose 1–31 explicit dates.")
    dates = [normalize_date(d) for d in dates]
    if len(set(dates)) != len(dates):
        raise ValueError("Planning dates must be unique.")
    configs = []
    for date in dates:
        target = datetime.strptime(date, "%d-%b-%Y").date()
        if target < now.date():
            raise ValueError("Planning dates must be today or in the future.")
        start = time_to_minutes(config.start_time)
        if target == now.date():
            start = max(start, ((now.hour * 60 + now.minute) // 30 + 1) * 30)
        if start + duration_minutes > time_to_minutes(config.end_time):
            configs.append(None)
        else:
            effective = replace(config, date=date, start_time=minutes_to_time(start))
            require_future_booking(effective, now=now)
            configs.append(effective)
    return {
        "config": asdict(config),
        "dates": dates,
        "configs": configs,
        "duration_minutes": duration_minutes,
        "mode": mode,
        "allow_split": allow_split,
        "transition_minutes": transition_minutes,
        "preferences": merge_preferences(preferences),
        "scope_sources": scope_sources or {},
    }


def planning_scope(payload):
    config = payload["config"]
    fields = ("buildings", "floors", "facility_types", "capacity", "equipment")
    sources = {
        key: payload.get("scope_sources", {}).get(key, "explicit_config")
        for key in fields
    }
    inherited = [
        key
        for key, source in sources.items()
        if source in {"saved_preferences", "built_in_default"}
    ]
    labels = "; ".join(
        f"{key}: {', '.join(config.get(key) or []) or 'unrestricted'}"
        for key in ("buildings", "floors", "facility_types", "equipment")
    )
    labels += f"; capacity: {config.get('capacity') or 'unrestricted'}"
    return {
        "dates": payload["dates"],
        "start_time": config["start_time"],
        "end_time": config["end_time"],
        "timezone": "Asia/Singapore",
        **{
            key: list(config.get(key) or []) if key != "capacity" else config.get(key)
            for key in fields
        },
        "filter_sources": sources,
        "inherited_filters": inherited,
        "avoided_rooms": payload["preferences"]["avoided_rooms"],
        "completeness_applies_to": "these_dates_windows_and_filters_only",
        "notice": f"Search scope — {labels}. Complete and ranking_complete apply only to this scope. Report inherited floor/type restrictions; exact facility labels do not imply coverage of other type variants.",
    }


def _free_intervals(snapshot, expected, avoided):
    if snapshot.config != expected:
        raise LayoutChanged("FBS planning response changed the requested search scope.")
    names = set()
    rooms = {}
    window_start, window_end = map(
        time_to_minutes, (expected.start_time, expected.end_time)
    )
    for room in snapshot.rooms:
        if (
            not isinstance(room.name, str)
            or not room.name.strip()
            or room.name.casefold() in names
        ):
            raise LayoutChanged("FBS planning rooms are ambiguous.")
        names.add(room.name.casefold())
        covered, free = window_start, []
        for slot in sorted(room.timeslots, key=lambda s: time_to_minutes(s.start)):
            start, end = time_to_minutes(slot.start), time_to_minutes(slot.end)
            if end <= start or slot.status not in {
                "free",
                "booked",
                "unavailable",
                "outside_booking_horizon",
            }:
                raise LayoutChanged("FBS planning interval is unreadable.")
            start, end = max(start, window_start), min(end, window_end)
            if end <= start:
                continue
            if start != covered:
                raise LayoutChanged(
                    "FBS planning intervals overlap or have missing coverage."
                )
            covered = end
            if slot.status == "free":
                if free and free[-1][1] == start:
                    free[-1] = (free[-1][0], end)
                else:
                    free.append((start, end))
        if covered != window_end:
            raise LayoutChanged("FBS planning calendar does not cover the full window.")
        if room.name.casefold() not in avoided:
            rooms[room.name] = free
    return rooms


def _covers(intervals, start, end):
    return any(a <= start and end <= b for a, b in intervals)


def _leg(room, start, end):
    return {
        "room": room,
        "start_time": minutes_to_time(start),
        "end_time": minutes_to_time(end),
    }


def _options(rooms, config, duration, split, gap):
    end_limit = time_to_minutes(config.end_time)
    for start in range(
        time_to_minutes(config.start_time), end_limit - duration + 1, 30
    ):
        singles = [
            name
            for name, free in rooms.items()
            if _covers(free, start, start + duration)
        ]
        for room in singles:
            yield start, [_leg(room, start, start + duration)]
        if singles or not split or start + duration + gap > end_limit:
            continue
        for first_duration in range(30, duration, 30):
            handover = start + first_duration
            first = [
                name for name, free in rooms.items() if _covers(free, start, handover)
            ]
            second = [
                name
                for name, free in rooms.items()
                if _covers(free, handover + gap, start + duration + gap)
            ]
            for a in first:
                for b in second:
                    if a != b:
                        yield start, [
                            _leg(a, start, handover),
                            _leg(b, handover + gap, start + duration + gap),
                        ]


def _rank(candidate, payload):
    prefs = payload["preferences"]
    preferred = {
        name.casefold(): (len(prefs["preferred_rooms"]) - i)
        / len(prefs["preferred_rooms"])
        for i, name in enumerate(prefs["preferred_rooms"])
    }
    dates = payload["dates"] if candidate.get("all_dates") else [candidate["date"]]
    legs = candidate["legs"]
    booked = sum(
        time_to_minutes(leg["end_time"]) - time_to_minutes(leg["start_time"])
        for leg in legs
    )
    room_score = (
        sum(
            preferred.get(leg["room"].casefold(), 0)
            * (time_to_minutes(leg["end_time"]) - time_to_minutes(leg["start_time"]))
            for leg in legs
        )
        / booked
    )
    start = time_to_minutes(legs[0]["start_time"])
    window_start = time_to_minutes(payload["config"]["start_time"])
    last_start = (
        time_to_minutes(payload["config"]["end_time"]) - payload["duration_minutes"]
    )
    if len(legs) > 1:
        last_start -= payload["transition_minutes"]
    fraction = (start - window_start) / max(1, last_start - window_start)
    time_score = (
        1
        if last_start == window_start
        else 1 - fraction if prefs["time_preference"] == "earliest" else fraction
    )
    components = {
        "room_preference": room_score,
        "time_preference": time_score,
        "date_preference": sum(
            1 - payload["dates"].index(date) / max(1, len(payload["dates"]) - 1)
            for date in dates
        )
        / len(dates),
        "single_room": float(len(legs) == 1),
    }
    weights = prefs["weights"]
    score = (
        sum(components[key] * weight for key, weight in weights.items())
        / (sum(weights.values()) or 1)
        * 100
    )
    candidate.update(
        score=round(score, 6),
        score_components={k: round(v, 6) for k, v in components.items()},
    )
    return (
        -candidate["score"],
        payload["dates"].index(dates[0]),
        start,
        tuple((leg["room"].casefold(), leg["end_time"]) for leg in legs),
    )


async def find_room_plans(payload, search, progress=_noop_progress):
    probes, calendars = [], {}
    avoided = {name.casefold() for name in payload["preferences"]["avoided_rooms"]}
    for date, config in zip(payload["dates"], payload["configs"]):
        if config is None:
            probes.append(
                {"date": date, "status": "elapsed_window", "rooms_checked": 0}
            )
            continue
        await progress(
            f"checking planning date {len(probes)+1}/{len(payload['dates'])}"
        )
        try:
            data = await search(config)
            snapshot = result_from_dict(data)
            calendars[date] = (
                _free_intervals(snapshot, config, avoided),
                config,
                snapshot.scraped_at,
            )
            probes.append(
                {
                    "date": date,
                    "status": "succeeded",
                    "rooms_checked": len(snapshot.rooms),
                    "booking_horizon": snapshot.booking_horizon,
                    "scraped_at": snapshot.scraped_at,
                    "effective_start_time": config.start_time,
                    "read_backend": data.get("read_backend"),
                    "matching_plan_available": next(
                        _options(
                            calendars[date][0],
                            config,
                            payload["duration_minutes"],
                            payload["allow_split"],
                            payload["transition_minutes"],
                        ),
                        None,
                    )
                    is not None,
                }
            )
        except (
            FBSError,
            PlaywrightError,
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            probes.append(
                {
                    "date": date,
                    "status": "failed",
                    "error": public_failure(error)
                    or {
                        "reason": "layout_changed",
                        "message": "Availability could not be verified for this date.",
                    },
                }
            )
    count = 0
    computation_limited = False

    def candidates():
        nonlocal count, computation_limited
        if payload["mode"] == "common":
            if len(calendars) != len(payload["dates"]):
                return
            common = None
            for rooms, config, _ in calendars.values():
                keys = {
                    (legs[0]["room"], start)
                    for start, legs in _options(
                        rooms, config, payload["duration_minutes"], False, 0
                    )
                }
                common = keys if common is None else common & keys
            for room, start in sorted(common or ()):
                if count == MAX_EVALUATED_CANDIDATES:
                    computation_limited = True
                    return
                count += 1
                legs = [_leg(room, start, start + payload["duration_minutes"])]
                yield {
                    "all_dates": True,
                    "legs": legs,
                    "room_changes": 0,
                }
        else:
            for date, (rooms, config, _) in calendars.items():
                for _, legs in _options(
                    rooms,
                    config,
                    payload["duration_minutes"],
                    payload["allow_split"],
                    payload["transition_minutes"],
                ):
                    if count == MAX_EVALUATED_CANDIDATES:
                        computation_limited = True
                        return
                    count += 1
                    yield {
                        "date": date,
                        "legs": legs,
                        "room_changes": len(legs) - 1,
                    }

    options = heapq.nsmallest(
        MAX_CANDIDATES, candidates(), key=lambda c: _rank(c, payload)
    )
    for index, candidate in enumerate(options):
        candidate["candidate_index"] = index
    failed = [p["date"] for p in probes if p["status"] == "failed"]
    return {
        "config": payload["config"],
        "effective_scope": planning_scope(payload),
        "dates": payload["dates"],
        "duration_minutes": payload["duration_minutes"],
        "mode": payload["mode"],
        "allow_split": payload["allow_split"],
        "transition_minutes": payload["transition_minutes"],
        "room_preferences": payload["preferences"],
        "candidates": options,
        "total": len(options),
        "total_matching_candidates": None if computation_limited else count,
        "matching_candidates_evaluated": count,
        "ranking_complete": not computation_limited,
        "candidate_limit": MAX_CANDIDATES,
        "truncated": count > MAX_CANDIDATES or computation_limited,
        "complete": not failed and not computation_limited,
        "partial_failure": bool(failed),
        "probes": probes,
        "scope": "Explicit dates, requested filters and duration; only future starts. Split plans include at most one room change and only where no single room fits that start. Transition gaps are additional to booked duration.",
        "submitted": False,
        "next_tool": "planned_booking_review" if options else None,
        "suggested_action": (
            "choose_candidate"
            if options
            else (
                "retry_failed_dates"
                if failed
                else (
                    "refine_planning_scope"
                    if computation_limited
                    else "inspect_date_restrictions_or_change_requested_scope"
                )
            )
        ),
    }
