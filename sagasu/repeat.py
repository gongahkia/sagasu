"""Read-only plans for repeating exact bookings or saved templates."""

from playwright.async_api import Error as PlaywrightError
from datetime import datetime

from .errors import public_failure
from .fbs_client import FBSError, SearchConfig
from .booking_details import booking_window
from .personal import SMU_EMAIL_RE


def verified_cobookers(details):
    people = details.get("co_bookers")
    if not isinstance(people, list) or not people:
        return None
    if all(isinstance(person, str) for person in people):
        emails = people
    elif details.get("co_bookers_coverage", {}).get("complete") is True and all(
        isinstance(person, dict) for person in people
    ):
        emails = [person.get("email") for person in people]
    else:
        return None
    if not 1 <= len(emails) <= 8 or any(
        not isinstance(email, str) or not SMU_EMAIL_RE.fullmatch(email)
        for email in emails
    ):
        return None
    normalized = [email.casefold() for email in emails]
    return normalized if len(set(normalized)) == len(normalized) else None


def normalize_date(value):
    return datetime.strptime(
        value, "%Y-%m-%d" if len(value) == 10 and value[4] == "-" else "%d-%b-%Y"
    ).strftime("%d-%b-%Y")


async def read_booking_details(client, reference):
    from .booking_details import read_booking_details as read_details

    return await read_details(
        client, reference, source="bookings", include_additional=True
    )


async def repeat_plan(client, payload):
    slots = []
    for source in payload["sources"]:
        try:
            details = (
                await read_booking_details(client, source["reference_number"])
                if "reference_number" in source
                else dict(source)
            )
            # Date/time/room are taken from the immutable source reviewed by the caller.
            identity = (
                ("room", "date", "start_time", "end_time")
                if "reference_number" in source
                else ("room", "start_time", "end_time")
            )
            if any(details.get(k) != source.get(k) for k in identity):
                raise FBSError("The source booking changed; refresh My Bookings")
            config = SearchConfig(
                payload["date"], source["start_time"], source["end_time"]
            )
            result = await client.search(config, room_name=source["room"])
            participants = verified_cobookers(details)
            slot = {
                "source": source,
                "details": details,
                "search": result.to_dict(),
                "available_rooms": [room.name for room in result.bookable_rooms],
                "missing_fields": [
                    key
                    for key in ("purpose", "co_booker")
                    if not details.get(key)
                    and not (key == "co_booker" and participants)
                ],
                "status": "available" if result.bookable_rooms else "unavailable",
                "submitted": False,
            }
            if participants:
                slot["booking_co_bookers"] = participants
            slots.append(slot)
        except (FBSError, PlaywrightError, TimeoutError, ValueError) as error:
            slots.append(
                {
                    "source": source,
                    "status": "failed",
                    "error": public_failure(error)
                    or {
                        "message": "Source details or availability could not be verified."
                    },
                    "submitted": False,
                }
            )
    return {
        "date": payload["date"],
        "timezone": "Asia/Singapore",
        "slots": slots,
        "partial_failure": any(s["status"] == "failed" for s in slots),
        "submitted": False,
        "next_tool": "repeat_booking_review",
        "submission_policy": "Each slot requires its own review and submission. Report each result independently; the group is not atomic.",
    }
