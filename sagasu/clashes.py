"""Bounded interval checks against a verified My Bookings snapshot."""

from datetime import datetime

from .booking_details import booking_window
from .fbs_client import (
    LayoutChanged,
    FBS_TIMEZONE,
    SearchConfig,
    require_future_booking,
    time_to_minutes,
    minutes_to_time,
)
from .repeat import normalize_date


def prepare_windows(windows):
    if not isinstance(windows, list) or not 1 <= len(windows) <= 31:
        raise ValueError("Choose 1–31 explicit proposed date/time windows.")
    result = []
    for window in windows:
        if not isinstance(window, dict) or set(window) != {
            "date",
            "start_time",
            "end_time",
        }:
            raise ValueError(
                "Each proposed window needs only date, start_time and end_time."
            )
        config = SearchConfig(
            normalize_date(window["date"]), window["start_time"], window["end_time"]
        )
        require_future_booking(config)
        result.append(
            {
                "date": config.date,
                "start_time": config.start_time,
                "end_time": config.end_time,
            }
        )
    return result


def overlap(a, b):
    return (
        a["date"] == b["date"]
        and time_to_minutes(a["start_time"]) < time_to_minutes(b["end_time"])
        and time_to_minutes(b["start_time"]) < time_to_minutes(a["end_time"])
    )


def clear_intervals(window, matches):
    """Subtract the union of known bookings from a completely read window."""
    cursor = time_to_minutes(window["start_time"])
    end = time_to_minutes(window["end_time"])
    intervals = []
    for start, stop in sorted(
        (time_to_minutes(row["start_time"]), time_to_minutes(row["end_time"]))
        for row in matches
    ):
        start, stop = max(cursor, start), min(end, stop)
        if start > cursor:
            intervals.append((cursor, start))
        cursor = max(cursor, stop)
    if cursor < end:
        intervals.append((cursor, end))
    return [
        {
            "start_time": minutes_to_time(start),
            "end_time": minutes_to_time(stop),
            "duration_minutes": stop - start,
        }
        for start, stop in intervals
    ]


def check_clashes(listing, windows):
    bookings = listing.get("bookings")
    total = listing.get("total")
    if (
        not isinstance(bookings, list)
        or type(total) is not int
        or total < len(bookings)
    ):
        raise LayoutChanged("The My Bookings clash-check coverage is inconsistent")
    refs = [row.get("reference_number") for row in bookings]
    if any(not isinstance(ref, str) or not ref for ref in refs) or len(
        set(refs)
    ) != len(refs):
        raise LayoutChanged("The My Bookings clash-check references are ambiguous")
    coverage_complete = listing.get("complete") is True and len(bookings) == total
    records, unreadable = [], []
    for row in bookings:
        status = row.get("fbs_status")
        if not isinstance(status, str) or not status.strip():
            raise LayoutChanged("A My Bookings status is unreadable")
        normalized = status.strip().casefold()
        if normalized in {"cancelled", "canceled", "withdrawn", "rejected"}:
            continue
        try:
            window = booking_window(row)
        except (ValueError, TypeError):
            unreadable.append(row["reference_number"])
            continue
        records.append(
            {
                "reference_number": row["reference_number"],
                "room": row.get("room"),
                "fbs_status": status,
                **window,
                "classification": (
                    "confirmed"
                    if normalized == "confirmed"
                    else (
                        "pending"
                        if normalized.startswith("pending")
                        else "unknown_status"
                    )
                ),
            }
        )
    checks = []
    for index, window in enumerate(windows):
        matches = [row for row in records if overlap(window, row)]
        uncertain = any(row["classification"] == "unknown_status" for row in matches)
        known = any(row["classification"] != "unknown_status" for row in matches)
        complete = coverage_complete and not unreadable and not uncertain
        clear = clear_intervals(window, matches) if complete else None
        checks.append(
            {
                "window_index": index,
                **window,
                "clear_of_existing_bookings": (
                    False if known else True if complete else None
                ),
                "overlaps": matches[:50],
                "overlap_count": len(matches),
                "overlaps_truncated": len(matches) > 50,
                "complete": complete,
                "clear_intervals": clear,
                "longest_clear_minutes": (
                    max((part["duration_minutes"] for part in clear), default=0)
                    if clear is not None
                    else None
                ),
            }
        )
    return {
        "windows": checks,
        "proposed_overlaps": [
            [i, j]
            for i, a in enumerate(windows)
            for j, b in enumerate(windows)
            if i < j and overlap(a, b)
        ],
        "booking_coverage_complete": coverage_complete,
        "bookings_read": len(bookings),
        "bookings_total": total,
        "unreadable_booking_references": unreadable,
        "complete": all(check["complete"] for check in checks),
        "scope": "Time overlaps with this account's My Bookings records, including pending requests. Clear intervals require complete coverage and subtract all overlaps, including records beyond the displayed overlap page. Cancelled/withdrawn/rejected records are excluded. Does not establish booker identity, room availability or final FBS eligibility.",
        "timezone": "Asia/Singapore",
        "checked_at": datetime.now(FBS_TIMEZONE).isoformat(),
        "read_backend": listing.get("read_backend"),
        "submitted": False,
    }
