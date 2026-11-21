"""Explicit, bounded alternatives. Never relax timing or filters implicitly."""

from dataclasses import asdict, replace
from playwright.async_api import Error as PlaywrightError

from .errors import public_failure
from .fbs_client import (
    SearchConfig,
    RoomAvailability,
    TimeSlot,
    SearchResult,
    require_future_booking,
    time_to_minutes,
    minutes_to_time,
    FBSError,
    BookingUnavailable,
    stops_remote_job,
)


def result_from_dict(result):
    return SearchResult(
        SearchConfig.from_mapping(result["config"]),
        [
            RoomAvailability(
                row["name"], [TimeSlot(**slot) for slot in row["timeslots"]]
            )
            for row in result["rooms"]
        ],
        result.get("scraped_at", ""),
        complete=result.get("complete", True),
    )


def candidates(source, shift_minutes=0, buildings=None, floors=None):
    config = SearchConfig.from_mapping(source["config"])
    base = replace(
        config,
        buildings=tuple(buildings) if buildings is not None else config.buildings,
        floors=tuple(floors) if floors is not None else config.floors,
    )
    choices = []
    if base != config:
        choices.append(base)
    for delta in range(30, shift_minutes + 1, 30):
        for signed in (delta, -delta):
            start = time_to_minutes(base.start_time) + signed
            end = time_to_minutes(base.end_time) + signed
            if 0 <= start < end <= 1440:
                candidate = replace(
                    base,
                    start_time=minutes_to_time(start),
                    end_time=minutes_to_time(end),
                )
                try:
                    require_future_booking(candidate)
                except BookingUnavailable:
                    continue
                choices.append(candidate)
    return choices[:6]


def report(source, probes, avoided_rooms=()):
    current = result_from_dict(source)
    horizon = current.booking_horizon
    blocked = bool(
        horizon and horizon["all_checked_rooms_affected"] and not current.bookable_rooms
    )
    return {
        "source_config": source["config"],
        "source_scraped_at": source.get("scraped_at"),
        "source_rooms_checked": len(current.rooms),
        "source_complete": current.complete,
        "current_available_rooms": [
            r.name
            for r in current.bookable_rooms
            if r.name.casefold() not in {n.casefold() for n in avoided_rooms}
        ],
        "booking_horizon": horizon,
        "probes": probes,
        "complete": current.complete is True
        and all(
            p.get("status") == "succeeded" and p.get("complete") is True for p in probes
        ),
        "partial_failure": any(p.get("status") == "failed" for p in probes),
        "coverage_scope": "Source and returned probe configurations only; counts describe checked rooms before preference exclusions, not other facilities.",
        "probe_limit": 6,
        "submitted": False,
        "suggested_action": (
            "retry_closer_to_date_or_explicitly_change_scope"
            if blocked and not probes
            else "review_available_candidates"
        ),
        "next_action": "Choose a returned configuration and run search_rooms to prepare a fresh review. No timing or location change is authorized by this result alone.",
        "next_tool": (
            "search_rooms" if any(p.get("available_rooms") for p in probes) else None
        ),
    }


async def find_alternatives(client, payload, *, search=None):
    search = search or client.search
    avoided = {name.casefold() for name in payload.get("avoided_rooms", ())}
    probes = []
    for config in payload["configs"]:
        try:
            result = await search(config)
            probes.append(
                {
                    "config": asdict(config),
                    "status": "succeeded",
                    "rooms_checked": len(result.rooms),
                    "complete": result.complete,
                    "available_rooms": [
                        r.name
                        for r in result.ranked_rooms(payload.get("preferred_rooms", ()))
                        if r.name.casefold() not in avoided
                    ],
                    "booking_horizon": result.booking_horizon,
                    "scraped_at": result.scraped_at,
                }
            )
        except (FBSError, PlaywrightError, TimeoutError) as error:
            if stops_remote_job(error):
                raise
            probes.append(
                {
                    "config": asdict(config),
                    "status": "failed",
                    "rooms_checked": None,
                    "complete": False,
                    "error": public_failure(error)
                    or {"message": "Availability could not be verified."},
                }
            )
    return report(payload["source"], probes, payload.get("avoided_rooms", ()))
