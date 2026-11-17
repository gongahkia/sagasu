"""Persist exact selected alternatives and refresh them without widening scope."""

import copy
from dataclasses import asdict, replace
from datetime import datetime

from playwright.async_api import Error as PlaywrightError

from .alternatives import result_from_dict
from .errors import public_failure
from .fbs_client import (
    FBS_TIMEZONE,
    FBSError,
    BookingUnavailable,
    SearchConfig,
    require_future_booking,
    time_to_minutes,
    stops_remote_job,
    _noop_progress,
)
from .personal import validate_preferences
from .repeat import normalize_date
from .room_plans import _free_intervals, _covers


def validate_shortlist(payload):
    if not isinstance(payload, dict) or set(payload) != {"config", "choices"}:
        raise ValueError(
            "A saved shortlist needs its original filters and exact choices."
        )
    config = payload["config"]
    if not isinstance(config, dict) or set(config) - {
        "date",
        "start_time",
        "end_time",
        "buildings",
        "floors",
        "facility_types",
        "capacity",
        "equipment",
    }:
        raise ValueError("Saved shortlist search fields are invalid.")
    filters = {}
    for key in ("buildings", "floors", "facility_types", "equipment"):
        values = config.get(key, [])
        if not isinstance(values, (list, tuple)):
            raise ValueError("Saved shortlist filters must be explicit lists.")
        filters[key] = list(values)
    validate_preferences(filters | {"room_capacity": config.get("capacity")})
    config = SearchConfig.from_mapping(config)
    choices = payload["choices"]
    if not isinstance(choices, list) or not 1 <= len(choices) <= 64:
        raise ValueError(
            "Save 1–64 exact choices, with at most 64 booking legs overall."
        )
    result, seen = [], set()
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) != {"date", "legs"}:
            raise ValueError("Each saved choice needs a date and exact room/time legs.")
        date, legs = normalize_date(choice["date"]), choice["legs"]
        if not isinstance(legs, list) or not 1 <= len(legs) <= 2:
            raise ValueError("Each saved choice can have at most one room change.")
        selected, previous_end = [], None
        for leg in legs:
            if not isinstance(leg, dict) or set(leg) != {
                "room",
                "start_time",
                "end_time",
            }:
                raise ValueError("Saved legs require exact room/start/end values.")
            room = leg["room"]
            if (
                not isinstance(room, str)
                or not 1 <= len(room.strip()) <= 200
                or any(ord(c) < 32 or ord(c) == 127 for c in room)
            ):
                raise ValueError("Use an exact single-line FBS room name.")
            window = replace(
                config,
                date=date,
                start_time=leg["start_time"],
                end_time=leg["end_time"],
            )
            start, end = time_to_minutes(window.start_time), time_to_minutes(
                window.end_time
            )
            if (
                start < time_to_minutes(config.start_time)
                or end > time_to_minutes(config.end_time)
                or previous_end is not None
                and (start < previous_end or start - previous_end > 120)
            ):
                raise ValueError(
                    "Saved legs must remain inside the original window, in order, with gaps no greater than two hours."
                )
            selected.append(
                {
                    "room": room.strip(),
                    "start_time": window.start_time,
                    "end_time": window.end_time,
                }
            )
            previous_end = end
        if (
            len(selected) == 2
            and selected[0]["room"].casefold() == selected[1]["room"].casefold()
        ):
            raise ValueError("A split choice must use two different rooms.")
        identity = (
            date,
            *(
                (leg["room"].casefold(), leg["start_time"], leg["end_time"])
                for leg in selected
            ),
        )
        if identity in seen:
            raise ValueError("Saved shortlist choices must be unique.")
        seen.add(identity)
        result.append({"date": date, "legs": selected})
    if (
        sum(len(choice["legs"]) for choice in result) > 64
        or len({choice["date"] for choice in result}) > 31
    ):
        raise ValueError("A shortlist is limited to 31 dates and 64 booking legs.")
    return {"config": asdict(config), "choices": result}


def shortlist_summary(payload):
    return {
        "dates": list(dict.fromkeys(choice["date"] for choice in payload["choices"])),
        "choice_count": len(payload["choices"]),
        "leg_count": sum(len(choice["legs"]) for choice in payload["choices"]),
        "availability": "Refresh before preparing any booking; saved choices are historical snapshots.",
    }


async def refresh_shortlist(payload, search, progress=_noop_progress, *, now=None):
    payload = validate_shortlist(payload)
    original = SearchConfig.from_mapping(payload["config"])
    choices = copy.deepcopy(payload["choices"])
    configs, probes, calendars = {}, [], {}
    for choice in choices:
        try:
            for leg in choice["legs"]:
                require_future_booking(
                    replace(
                        original,
                        date=choice["date"],
                        start_time=leg["start_time"],
                        end_time=leg["end_time"],
                    ),
                    now=now,
                )
        except BookingUnavailable:
            choice["status"] = "elapsed"
            continue
        date = choice["date"]
        start, end = choice["legs"][0]["start_time"], choice["legs"][-1]["end_time"]
        if date in configs:
            start, end = min(start, configs[date].start_time), max(
                end, configs[date].end_time
            )
        configs[date] = replace(original, date=date, start_time=start, end_time=end)
    for date, config in configs.items():
        await progress(f"refreshing saved date {len(probes)+1}/{len(configs)}")
        try:
            data = await search(config)
            snapshot = result_from_dict(data)
            calendars[date] = _free_intervals(snapshot, config, set())
            probes.append(
                {
                    "date": date,
                    "status": "succeeded",
                    "scraped_at": snapshot.scraped_at,
                    "booking_horizon": snapshot.booking_horizon,
                    "read_backend": data.get("read_backend"),
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
            if stops_remote_job(error):
                raise
            probes.append(
                {
                    "date": date,
                    "status": "failed",
                    "error": public_failure(error)
                    or {"message": "Saved-choice availability could not be verified."},
                }
            )
    candidates = []
    for index, choice in enumerate(choices):
        choice["choice_index"] = index
        if choice.get("status") == "elapsed":
            continue
        try:
            for leg in choice["legs"]:
                require_future_booking(
                    replace(
                        original,
                        date=choice["date"],
                        start_time=leg["start_time"],
                        end_time=leg["end_time"],
                    ),
                    now=now,
                )
        except BookingUnavailable:
            choice["status"] = "elapsed"
            continue
        if choice["date"] not in calendars:
            choice["status"] = "failed"
            continue
        rooms = calendars[choice["date"]]
        free = all(
            _covers(
                rooms.get(leg["room"], []),
                time_to_minutes(leg["start_time"]),
                time_to_minutes(leg["end_time"]),
            )
            for leg in choice["legs"]
        )
        choice["status"] = "available" if free else "unavailable_in_saved_scope"
        if free:
            candidates.append(
                {
                    "candidate_index": len(candidates),
                    "saved_choice_index": index,
                    "date": choice["date"],
                    "legs": copy.deepcopy(choice["legs"]),
                    "room_changes": len(choice["legs"]) - 1,
                }
            )
    failed = any(probe["status"] == "failed" for probe in probes)
    return {
        "config": payload["config"],
        "dates": list(dict.fromkeys(c["date"] for c in choices)),
        "shortlist": choices,
        "candidates": candidates,
        "total": len(candidates),
        "complete": not failed,
        "partial_failure": failed,
        "probes": probes,
        "scope": "Exact saved choices with their original FBS filters. No alternative rooms, dates or times were introduced. Availability is a fresh snapshot, not a reservation.",
        "checked_at": datetime.now(FBS_TIMEZONE).isoformat(),
        "submitted": False,
        "next_tool": "planned_booking_review" if candidates else None,
    }
