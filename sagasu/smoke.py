"""Explicitly invoked live, read-only FBS compatibility check. Never submits forms."""

import asyncio

from .fbs_account import read_bookings
from .fbs_client import FBSClient, search_config_from_preferences
from .personal import PersonalStore
from .runtime import InstanceLock


async def run_smoke(settings, *, timeout=180):
    report = {"ok": False, "live": True, "read_only": True, "checks": []}
    stage = "session"
    try:
        with InstanceLock(settings.profile):
            async with asyncio.timeout(timeout):
                # interactive=False never submits credentials or attempts MFA renewal.
                async with FBSClient(interactive=False, keep_alive=False, profile_path=settings.profile) as client:
                    await client.ensure_authenticated()
                    report["checks"].append({"name": stage, "status": "pass"})
                    stage = "search"
                    config = search_config_from_preferences(PersonalStore(settings.preferences).preferences())
                    result = await client.search(config)
                    report["checks"].append({"name": stage, "status": "pass", "rooms_read": len(result.rooms), "bookable_rooms": len(result.bookable_rooms)})
                    stage = "bookings"
                    bookings = await read_bookings(client)
                    report["checks"].append({"name": stage, "status": "pass", "rows_read": len(bookings["bookings"]), "total": bookings["total"], "complete": bookings["complete"]})
        report["ok"] = True
    except Exception as error:
        # Do not echo browser content, account rows, identity, cookies or credentials.
        report["checks"].append({"name": stage, "status": "fail", "error_code": type(error).__name__,
            "hint": "Stop the MCP server, verify FBS reachability, run sagasu login if needed, and retry this explicit live check."})
    return report
