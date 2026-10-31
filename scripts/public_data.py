"""Validate and reconstruct the allowlisted public JSON contract (stdlib only)."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

MESSAGES = {
    "never": "No availability snapshot has been published yet.",
    "ok": "Daily availability refresh completed.",
    "authentication_required": "SMU sign-in needs attention. Showing the last successful snapshot.",
    "layout_changed": "FBS availability could not be read reliably. Showing the last successful snapshot.",
    "failed": "The daily refresh failed. Showing the last successful snapshot.",
}


def timestamp(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError("Expected an aware timestamp")
    return value


def minutes(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("Invalid public interval time")
    hours, minute = map(int, value.split(":"))
    if not (0 <= hours <= 23 and 0 <= minute <= 59):
        raise ValueError("Invalid public interval time")
    return hours * 60 + minute


def sanitize_snapshot(value):
    if value is None:
        return None
    if (
        value.get("version") != 1
        or value.get("timezone") != "Asia/Singapore"
        or len(value.get("days", [])) != 7
    ):
        raise ValueError("Expected a complete seven-day public snapshot")
    days = []
    for source in value["days"]:
        current = date.fromisoformat(source["date"])
        if days and current != date.fromisoformat(days[-1]["date"]) + timedelta(days=1):
            raise ValueError("Snapshot days must be consecutive and ordered")
        if (source["start_time"], source["end_time"]) != ("08:00", "22:00"):
            raise ValueError("Unexpected public search window")
        rooms = []
        seen = set()
        for room in source["rooms"]:
            name = room["name"]
            if not isinstance(name, str) or not name.strip() or name in seen:
                raise ValueError("Expected unique public room names")
            seen.add(name)
            slots = []
            cursor = 480
            for slot in room["timeslots"]:
                start, end = minutes(slot["start"]), minutes(slot["end"])
                if (
                    start != cursor
                    or not start < end <= 1320
                    or slot["status"] not in {"free", "booked", "unavailable"}
                ):
                    raise ValueError("Incomplete or invalid public room intervals")
                slots.append({key: slot[key] for key in ("start", "end", "status")})
                cursor = end
            if cursor != 1320:
                raise ValueError("Public room intervals do not cover the full window")
            rooms.append({"name": name, "timeslots": slots})
        days.append(
            {
                "date": current.isoformat(),
                "start_time": "08:00",
                "end_time": "22:00",
                "rooms": rooms,
            }
        )
    return {
        "version": 1,
        "updated_at": timestamp(value["updated_at"]),
        "timezone": "Asia/Singapore",
        "days": days,
    }


def sanitize_status(value):
    status = value["status"]
    if status not in MESSAGES:
        raise ValueError("Unknown public refresh status")
    # Canonical messages prevent arbitrary diagnostics/page text from reaching Pages.
    return {
        "status": status,
        "attempted_at": timestamp(value.get("attempted_at"), optional=True),
        "message": MESSAGES[status],
    }


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
