"""Classify a retained booking listing in SGT without changing its FBS status."""

from datetime import datetime, timedelta

from .booking_details import booking_window
from .fbs_client import FBS_TIMEZONE, time_to_minutes


def annotate_booking_timeline(listing, *, now):
    now = (
        now.replace(tzinfo=FBS_TIMEZONE)
        if now.tzinfo is None
        else now.astimezone(FBS_TIMEZONE)
    )
    source = listing.get("bookings")
    rows, upcoming, ongoing, references = [], [], [], []
    unclassified = 0
    if not isinstance(source, list):
        source = []
        unclassified = 1
    for original in source:
        if not isinstance(original, dict):
            rows.append(original)
            unclassified += 1
            continue
        row = dict(original)
        reference = row.get("reference_number")
        references.append(reference)
        valid_reference = isinstance(reference, str) and bool(reference.strip())
        status = " ".join(str(row.get("fbs_status") or "").split()).casefold()
        cancelled = status in {"cancelled", "canceled", "withdrawn", "rejected"}
        known_status = (
            cancelled or status == "confirmed" or status.startswith("pending")
        )
        try:
            window = booking_window(row)
            day = datetime.strptime(window["date"], "%d-%b-%Y").replace(
                tzinfo=FBS_TIMEZONE
            )
            start, end = (
                day + timedelta(minutes=time_to_minutes(window[key]))
                for key in ("start_time", "end_time")
            )
            timing = "upcoming" if start > now else "ongoing" if now < end else "past"
            row.update(
                starts_at=start.isoformat(timespec="seconds"),
                ends_at=end.isoformat(timespec="seconds"),
                temporal_status=timing,
            )
            if valid_reference and known_status and not cancelled:
                if timing == "upcoming":
                    upcoming.append((start, reference))
                elif timing == "ongoing":
                    ongoing.append((start, reference))
            if not valid_reference or not known_status:
                unclassified += 1
        except (ValueError, TypeError, KeyError):
            row.update(starts_at=None, ends_at=None, temporal_status="unreadable")
            # A cancelled row cannot precede the next eligible booking, even
            # when FBS no longer exposes its old date/time.
            if not cancelled or not valid_reference:
                unclassified += 1
        rows.append(row)
    complete = (
        listing.get("complete") is True
        and type(listing.get("total")) is int
        and listing["total"] == len(source)
        and all(isinstance(ref, str) and ref.strip() for ref in references)
        and len(set(ref for ref in references if isinstance(ref, str)))
        == len(references)
        and unclassified == 0
    )
    return {
        **listing,
        "bookings": rows,
        "timeline": {
            "as_of": now.isoformat(timespec="seconds"),
            "timezone": "Asia/Singapore",
            "complete": complete,
            "next_upcoming_reference": (
                min(upcoming)[1] if complete and upcoming else None
            ),
            "next_upcoming_status": (
                "unknown" if not complete else "found" if upcoming else "none"
            ),
            "ongoing_references": [reference for _, reference in sorted(ongoing)],
            "unclassified_count": unclassified,
            "selection_rule": "Next upcoming means the earliest non-cancelled booking starting strictly after as_of; ongoing bookings are separate. Selection covers the full retained listing before MCP pagination. FBS statuses remain a snapshot of the original read.",
        },
    }
