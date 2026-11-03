"""Read-only evidence for uncertain writes. Absence is never proof of success."""

from .repeat import booking_window
from .fbs_account import read_bookings, read_task_list


def identity(kind, summary):
    keys = (
        ("room", "date", "start_time", "end_time")
        if kind == "book"
        else ("reference_number",)
    )
    if not all(isinstance(summary.get(key), str) and summary[key] for key in keys):
        return None
    return (kind, *(summary[key].strip().casefold() for key in keys))


def evidence(state, *, verified=False, reference=None, status=None, complete=False):
    return {
        "state": state,
        "action_outcome_verified": verified,
        "safe_to_retry": False,
        "reference_number": reference,
        "fbs_status": status,
        "source_complete": complete,
        "message": {
            "confirmed": "The exact booking reference is confirmed in FBS.",
            "pending_confirmation": "The exact booking reference exists and still requires confirmation.",
            "cancelled": "The exact reference is marked cancelled in FBS.",
            "approved": "The exact task is marked approved; booking confirmation is a separate status.",
            "matching_booking_found": "A matching booking exists, but it cannot be attributed to the interrupted action without its reference. Do not submit another booking.",
            "not_found": "No matching record was found in the checked rows. Absence does not prove the action failed or a cancellation succeeded.",
            "unresolved": "The current records do not prove the action outcome. Inspect FBS before any retry.",
            "ambiguous": "Multiple records match. The interrupted action cannot be uniquely reconciled.",
        }[state],
    }


async def reconcile(client, payload):
    kind = payload["kind"]
    summary = payload["summary"]
    known_ref = payload.get("reference_number") or summary.get("reference_number")
    if kind == "approve_task":
        listing = await read_task_list(client)
        matches = [r for r in listing["tasks"] if r["reference_number"] == known_ref]
        if len(matches) > 1:
            return evidence("ambiguous", complete=listing["complete"])
        if len(matches) == 1 and matches[0]["status"].strip().casefold() == "approved":
            result = evidence(
                "approved",
                verified=True,
                reference=known_ref,
                complete=listing["complete"],
            )
            result["booking_status_verified"] = False
            return result
        return evidence(
            "unresolved" if matches else "not_found",
            reference=known_ref,
            complete=listing["complete"],
        )
    listing = await read_bookings(client)
    matches = []
    for row in listing["bookings"]:
        if known_ref:
            if row["reference_number"] == known_ref:
                matches.append(row)
        elif kind == "book":
            try:
                window = booking_window(row)
            except ValueError:
                continue
            if identity("book", {**row, **window}) == identity("book", summary):
                matches.append(row)
    if not matches:
        return evidence("not_found", reference=known_ref, complete=listing["complete"])
    if len(matches) != 1:
        return evidence("ambiguous", complete=listing["complete"])
    row = matches[0]
    status = row["fbs_status"]
    normalized = status.strip().casefold()
    if kind == "book":
        try:
            match_identity = identity("book", {**row, **booking_window(row)})
        except ValueError:
            match_identity = None
        if match_identity != identity("book", summary):
            return evidence(
                "unresolved",
                reference=known_ref,
                status=status,
                complete=listing["complete"],
            )
        if not known_ref:
            return evidence(
                "matching_booking_found",
                reference=row["reference_number"],
                status=status,
                complete=listing["complete"],
            )
        if normalized in {"confirmed", "pending confirmation"}:
            return evidence(
                "confirmed" if normalized == "confirmed" else "pending_confirmation",
                verified=True,
                reference=known_ref,
                status=status,
                complete=listing["complete"],
            )
    if kind == "cancel" and normalized in {"cancelled", "canceled"}:
        # Reference plus immutable identity guards a changed/stale source record.
        if any(
            row.get(k) != summary.get(k)
            for k in ("room", "date_time")
            if summary.get(k)
        ):
            return evidence(
                "unresolved",
                reference=known_ref,
                status=status,
                complete=listing["complete"],
            )
        return evidence(
            "cancelled",
            verified=True,
            reference=known_ref,
            status=status,
            complete=listing["complete"],
        )
    return evidence(
        "unresolved", reference=known_ref, status=status, complete=listing["complete"]
    )
