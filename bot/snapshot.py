"""Public snapshot contract: deliberately excludes raw FBS event details."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .fbs_client import SearchConfig

SGT = ZoneInfo("Asia/Singapore")


def search_days(now=None):
    today = (now or datetime.now(SGT)).astimezone(SGT).date()
    return [
        SearchConfig(
            date=(today + timedelta(days=offset)).strftime("%d-%b-%Y"),
            start_time="08:00",
            end_time="22:00",
            facility_types=("Group Study Room",),
        )
        for offset in range(7)
    ]


def public_snapshot(results, *, now=None):
    configs = [result.config for result in results]
    if len(results) != 7 or len({config.date for config in configs}) != 7:
        raise ValueError("A complete seven-day scrape is required")
    return {
        "version": 1,
        "updated_at": (now or datetime.now(SGT)).isoformat(),
        "timezone": "Asia/Singapore",
        "days": [
            {
                "date": datetime.strptime(result.config.date, "%d-%b-%Y").date().isoformat(),
                "start_time": result.config.start_time,
                "end_time": result.config.end_time,
                "rooms": [
                    {
                        "name": room.name,
                        "timeslots": [
                            {"start": slot.start, "end": slot.end, "status": slot.status}
                            for slot in room.timeslots
                        ],
                    }
                    for room in sorted(result.rooms, key=lambda item: item.name.casefold())
                ],
            }
            for result in results
        ],
    }


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
