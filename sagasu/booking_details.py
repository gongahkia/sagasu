"""Reference-verified, read-only booking detail extraction from FBS VIEW pages."""

import asyncio
import re
from datetime import datetime

from playwright.async_api import Error as PlaywrightError

from .fbs_client import FBSError, FBS_TIMEZONE, LayoutChanged, SearchConfig
from .personal import SMU_EMAIL_RE

MAX_FIELD_LENGTH = 8192
MAX_PARTICIPANTS = 200

FIELDS = {
    "reference_number": (
        "ObjectNumber",
        ("Ref Number", "Ref. Number", "Reference Number"),
    ),
    "purpose": ("TextboxPurpose", ("Purpose",)),
    "on_behalf_of": ("DropOnbehalfIndicator", ("Book For", "On Behalf Of")),
    "building": ("LabelBuilding", ("Building",)),
    "room": ("LabelFacilityName", ("Facility", "Facility Name", "Room")),
    "floor": ("LabelFloor", ("Floor",)),
    "facility_type": ("LabelPassType", ("Facility Type",)),
    "date": ("BookingDate", ("Date Requested", "Booking Date", "Date")),
    "start_time": ("DropDownStartTime", ("Time From", "From", "Start Time")),
    "end_time": ("DropDownEndTime", ("To", "Time To", "End Time")),
    "created_by": ("CreatedBy", ("First Created By", "Created By")),
    "confirmed_by": ("ConfirmedBy", ("Confirmed By",)),
    "approved_by": ("ApprovedBy", ("Approved By",)),
}

# These values are returned only when their exposure has been requested.
ADDITIONAL_FIELDS = {
    "credits_deducted": ("BookingCreditsDeducted_Label", ("Booking Credits Deducted",)),
    "use_type": ("DropDownUsageType", ("Use Type", "Usage Type")),
    "booking_usage": ("DropDownSpaceBookingUsage", ("Booking Usage",)),
    "modified_by": ("ModifiedBy", ("Last Modified By", "Modified By")),
}


class BookingDetailsUnavailable(FBSError):
    reason = "booking_details_unavailable"


def booking_window(record):
    match = re.search(
        r"(\d{2}-[A-Za-z]{3}-\d{4})(?:\s*\([^)]*\))?\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})",
        record.get("date_time", ""),
    )
    if not match:
        raise ValueError("Booking date/time could not be verified")
    config = SearchConfig(match[1], match[2], match[3])
    return {
        "date": config.date,
        "start_time": config.start_time,
        "end_time": config.end_time,
    }


def clean(value):
    return " ".join(value.split())


def field_value(snapshot, key, definitions=FIELDS):
    candidates = snapshot.get("fields", {}).get(key, [])
    if not candidates:
        return None, "not_exposed"
    values = []
    # Editable controls and read-only text are both used by the live FBS site.
    controls = [item for item in candidates if item.get("control")]
    for item in controls or candidates:
        value = item.get("value")
        if not isinstance(value, str) or len(value) > MAX_FIELD_LENGTH:
            return None, "unreadable"
        if not item.get("control"):
            caption, separator, remainder = value.partition(":")
            if separator and clean(caption).casefold() in {
                label.casefold() for label in definitions[key][1]
            }:
                value = remainder
        values.append(value.strip())
    if len(set(values)) != 1:
        return None, "ambiguous"
    value = values[0]
    return value or None, "available" if value else "empty"


def emails(value):
    return {
        email.casefold()
        for email in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", value)
        if SMU_EMAIL_RE.fullmatch(email)
    }


def audit_actor(value):
    if not value:
        return None, None
    match = re.fullmatch(
        r"(.+?)\s+on\s+(\d{2}-[A-Za-z]{3}-\d{4})\s+at\s+(\d{2}:\d{2}:\d{2})(?:\s+\([^\n]*\))?",
        clean(value),
        re.I,
    )
    actor = match[1] if match else clean(value)
    timestamp = None
    if match:
        try:
            timestamp = (
                datetime.strptime(f"{match[2]} {match[3]}", "%d-%b-%Y %H:%M:%S")
                .replace(tzinfo=FBS_TIMEZONE)
                .isoformat(timespec="seconds")
            )
        except ValueError:
            pass
    found = emails(actor)
    return {
        "name": actor,
        "email": next(iter(found)) if len(found) == 1 else None,
    }, timestamp


def parse_participants(snapshot):
    data = snapshot.get("participants")
    if data is None:
        return None, {"status": "not_exposed", "complete": None, "total": None}
    headers = [clean(label).casefold() for label in data.get("headers", [])]
    raw_rows = data.get("rows", [])
    totals = re.findall(r"(\d+)\s+ITEM\(S\)\s+TOTAL", data.get("summary", ""), re.I)
    if len(set(totals)) > 1 or len(raw_rows) > MAX_PARTICIPANTS:
        return None, {"status": "unreadable", "complete": False, "total": None}
    total = int(totals[0]) if totals else None
    columns = {
        "name": {"name", "co-booker", "co booker"},
        "email": {"email", "email address", "e-mail"},
        "confirmation_status": {"status", "confirmation status"},
    }
    positions = {}
    for key, labels in columns.items():
        indices = [index for index, header in enumerate(headers) if header in labels]
        if len(indices) > 1:
            return None, {"status": "ambiguous", "complete": False, "total": total}
        positions[key] = indices[0] if indices else None
    if positions["name"] is None and positions["email"] is None:
        return None, {"status": "unreadable", "complete": False, "total": total}
    result = []
    for row in raw_rows:
        if len(row) == 1 and clean(row[0]).casefold() == "no items":
            if total is None:
                total = 0
            continue
        if len(row) != len(headers) or any(
            not isinstance(cell, str) or len(cell) > MAX_FIELD_LENGTH for cell in row
        ):
            return None, {"status": "unreadable", "complete": False, "total": total}
        values = {
            key: clean(row[position]) or None if position is not None else None
            for key, position in positions.items()
        }
        if not values["name"] and not values["email"]:
            continue
        found = emails(values["email"] or values["name"] or "")
        values["email"] = next(iter(found)) if len(found) == 1 else None
        result.append(values)
    if total is not None and (len(result) > total or total > 0 and not result):
        return None, {"status": "unreadable", "complete": False, "total": total}
    complete = len(result) == total if total is not None else None
    return result, {
        "status": "available" if complete else "partial",
        "complete": complete,
        "total": total,
    }


def parse_booking_details(snapshot, record, *, include_additional=False):
    reference, reference_status = field_value(snapshot, "reference_number")
    if reference_status != "available" or reference != record["reference_number"]:
        raise LayoutChanged(
            "The booking detail reference did not match the requested booking"
        )
    values, statuses = {}, {}
    for key in FIELDS:
        values[key], statuses[key] = field_value(snapshot, key)
    window = booking_window(record)
    for key in ("date", "start_time", "end_time"):
        if values[key]:
            displayed = (
                re.match(r"^\d{2}:\d{2}", values[key])[0]
                if key == "end_time" and re.match(r"^\d{2}:\d{2}", values[key])
                else values[key]
            )
            if displayed != window[key]:
                raise LayoutChanged(
                    "The booking date/time changed; refresh the source listing"
                )
    if values["room"] and clean(values["room"]) != clean(record["room"]):
        raise LayoutChanged("The booking facility changed; refresh the source listing")
    booked_by, created_at = audit_actor(values.pop("created_by"))
    participants, coverage = parse_participants(snapshot)
    confirmed, confirmed_at = audit_actor(values.pop("confirmed_by"))
    approved, approved_at = audit_actor(values.pop("approved_by"))
    result = {
        **record,
        **window,
        "purpose": values["purpose"],
        "building": values["building"],
        "floor": values["floor"],
        "facility_type": values["facility_type"],
        "on_behalf_of": values["on_behalf_of"],
        "booked_by": booked_by,
        "booked_by_source": next(
            (
                item.get("scope", "booking_form")
                for item in snapshot.get("fields", {}).get("created_by", [])
                if item.get("value")
            ),
            None,
        ),
        "created_at": created_at,
        "co_bookers": participants,
        "co_bookers_coverage": coverage,
        "co_booker": (
            participants[0]["email"]
            if participants and len(participants) == 1 and coverage["complete"]
            else None
        ),
        "confirmed_by": [confirmed] if confirmed else None,
        "confirmed_at": confirmed_at,
        "approved_by": [approved] if approved else None,
        "approved_at": approved_at,
        "approval_history": None,
        "field_status": {
            **statuses,
            "booked_by": statuses["created_by"],
            "co_bookers": coverage["status"],
            "approval_history": "not_exposed",
        },
        "reference_verified": True,
        "timezone": "Asia/Singapore",
        "scraped_at": datetime.now(FBS_TIMEZONE).isoformat(timespec="seconds"),
        "read_backend": {"name": "browser", "protocol": "ui_view"},
        "workflow_comments_included": False,
    }
    if include_additional:
        additional = {}
        for key in ADDITIONAL_FIELDS:
            additional[key], result["field_status"][key] = field_value(
                snapshot, key, ADDITIONAL_FIELDS
            )
        modified, modified_at = audit_actor(additional.pop("modified_by"))
        result["additional_details"] = {
            **additional,
            "modified_by": modified,
            "modified_at": modified_at,
        }
    return result


DETAIL_SNAPSHOT = r"""({reference, fields}) => {
    const visible = e => !!e.getClientRects().length && e.getAttribute('aria-hidden') !== 'true';
    const roots = [...document.querySelectorAll('[id$="bookingFormControl1_ObjectNumber"]')].filter(visible);
    const identity = e => e.innerText.replace(/^\s*(?:Ref\.?\s*Number|Reference Number)\s*:\s*/i,'').trim();
    const matches = roots.filter(e => identity(e) === reference ||
        [...e.querySelectorAll('*')].some(n => n.children.length === 0 && n.innerText.trim() === reference));
    if (matches.length !== 1) return null;
    const prefix = matches[0].id.slice(0,-'ObjectNumber'.length);
    const values = {};
    for (const [key, base] of Object.entries(fields)) {
        let scope = 'booking_form';
        let nodes = [...document.querySelectorAll('#'+CSS.escape(prefix+base)+'_c1, #'+CSS.escape(prefix+base))].filter(visible);
        if (!nodes.length && roots.length === 1 && ['created_by','modified_by'].includes(key)) {
            scope = 'workflow_object_audit';
            nodes = [...document.querySelectorAll('#'+CSS.escape('AuditComponent_'+base)+'_c1, #'+CSS.escape('AuditComponent_'+base))].filter(visible);
        }
        values[key] = nodes.map(e => ({scope,control:['INPUT','TEXTAREA','SELECT'].includes(e.tagName),
            value:e.tagName === 'SELECT' ? [...e.selectedOptions].map(o=>o.text).join(' ') :
                ['INPUT','TEXTAREA'].includes(e.tagName) ? e.value : e.innerText}));
    }
    const tables = [...document.querySelectorAll('#'+CSS.escape(prefix+'GridCoBookers_gv'))].filter(visible);
    let participants = null;
    if (tables.length) {
        const headerSets = tables.map(t=>[...t.querySelectorAll('th')].map(e=>e.innerText.trim())).filter(h=>h.length);
        if (new Set(headerSets.map(h=>JSON.stringify(h))).size > 1) return null;
        participants = {headers:headerSets[0] || [],rows:tables.flatMap(t=>[...t.rows].filter(r=>!r.querySelector('th')).map(r=>[...r.cells].map(e=>e.innerText.trim()))),
            summary:document.getElementById(prefix+'GridCoBookers')?.innerText || ''};
    }
    return {fields:values, participants, prefix, visible_booking_forms:roots.length};
}"""


async def snapshot_booking_details(frame, reference, *, include_additional=False):
    definitions = {**FIELDS, **(ADDITIONAL_FIELDS if include_additional else {})}
    snapshot = await frame.evaluate(
        DETAIL_SNAPSHOT,
        {
            "reference": reference,
            "fields": {key: definition[0] for key, definition in definitions.items()},
        },
    )
    if not isinstance(snapshot, dict):
        raise LayoutChanged(
            "The requested booking detail view could not be uniquely verified"
        )
    return snapshot


HISTORY_GRID = "objectBase_ActivityHistoryComponent_gridWorkStatusHistory"
HISTORY_LINK = "a#objectBase_ButtonViewWorkflowHistory:visible"
HISTORY_CLOSE = "a#objectBase_ActivityHistoryDialogBox_bc:visible"

HISTORY_SNAPSHOT = r"""grid => {
    const visible = e => !!e.getClientRects().length;
    const tables = [...document.querySelectorAll('#'+CSS.escape(grid+'_gv'))].filter(visible);
    if (!tables.length) return null;
    const headerSets = tables.map(t=>[...t.querySelectorAll('th')].map(n=>n.innerText.trim())).filter(h=>h.length);
    if (new Set(headerSets.map(h=>JSON.stringify(h))).size !== 1) return null;
    const headers = headerSets[0];
    const allowed = new Set(['action','user','date/time']);
    return {headers, rows:tables.flatMap(t=>[...t.rows].filter(r=>!r.querySelector('th')).map(r=>[...r.cells].map((c,i)=>allowed.has(headers[i]?.toLowerCase()) ? c.innerText.trim() : ''))),
        summary:document.getElementById(grid+'_pager')?.innerText || ''};
}"""


def parse_history(snapshot):
    if not isinstance(snapshot, dict):
        raise LayoutChanged("FBS workflow history could not be read")
    headers = [clean(label).casefold() for label in snapshot.get("headers", [])]
    required = ("action", "user", "date/time")
    if any(headers.count(label) != 1 for label in required):
        raise LayoutChanged("FBS workflow history columns could not be verified")
    totals = re.findall(r"(\d+)\s+ITEM\(S\)\s+TOTAL", snapshot.get("summary", ""), re.I)
    if len(set(totals)) != 1:
        raise LayoutChanged("FBS workflow history total could not be verified")
    total = int(totals[0])
    events = []
    for row in snapshot.get("rows", []):
        if total == 0 and (
            not any(row) or len(row) == 1 and clean(row[0]).casefold() == "no items"
        ):
            continue
        if len(row) != len(headers):
            raise LayoutChanged("FBS workflow history row is incomplete")
        action, actor, raw_at = [clean(row[headers.index(label)]) for label in required]
        if (
            not action
            or not actor
            or any(len(value) > MAX_FIELD_LENGTH for value in (action, actor, raw_at))
        ):
            raise LayoutChanged("FBS workflow history identity is incomplete")
        try:
            at = (
                datetime.strptime(raw_at, "%d-%b-%Y %H:%M")
                .replace(tzinfo=FBS_TIMEZONE)
                .isoformat(timespec="minutes")
            )
        except ValueError:
            at = None
        person, _ = audit_actor(actor)
        events.append(
            {"action": action, "actor": person, "at": at, "display_time": raw_at}
        )
    if len(events) > total or total and not events:
        raise LayoutChanged("FBS workflow history does not agree with its total")
    return {
        "events": events,
        "total": total,
        "complete": len(events) == total,
        "comments_included": False,
    }


def apply_history(details, history):
    details["approval_history"] = history
    details["field_status"]["approval_history"] = (
        "available" if history["complete"] else "partial"
    )
    for key, pattern in (
        ("confirmed", r"(?:confirm(?:ed)?(?: booking)?|booking confirmed)"),
        ("approved", r"(?:approv(?:e|ed)(?: booking)?|booking approved)"),
    ):
        events = [
            event
            for event in history["events"]
            if re.fullmatch(pattern, event["action"], re.I)
        ]
        if not events and details.get(key + "_by"):
            continue
        people = {
            ((event["actor"]["name"] or "").casefold(), event["actor"]["email"]): event[
                "actor"
            ]
            for event in events
        }
        details[key + "_by"] = (
            list(people.values()) if events or history["complete"] else None
        )
        details[key + "_at"] = events[0]["at"] if len(events) == 1 else None
        details["field_status"][key + "_by"] = (
            "available"
            if events
            else "empty" if history["complete"] else "not_observed_in_partial_history"
        )
    return details


async def wait_for_details(client, reference, *, include_additional=True):
    from .fbs_http import fbs_document_url

    async with asyncio.timeout(45):
        while True:
            frame = client.page.frame(name="frameContent")
            try:
                if (
                    frame
                    and await frame.locator(
                        '[id$="bookingFormControl1_ObjectNumber"]:visible'
                    ).count()
                ):
                    if not fbs_document_url(frame.url):
                        raise LayoutChanged(
                            "Booking details opened outside the trusted FBS origin"
                        )
                    await client._wait_for_idle(frame)
                    return frame, await snapshot_booking_details(
                        frame, reference, include_additional=include_additional
                    )
            except PlaywrightError:
                if frame and not frame.is_detached():
                    raise
            await client.page.wait_for_timeout(250)


async def collect_participants(
    client, frame, reference, snapshot, *, include_additional
):
    from .fbs_paging import move_page

    participants, coverage = parse_participants(snapshot)
    if participants is None or coverage["complete"] is not False:
        return snapshot
    grid = snapshot["prefix"] + "GridCoBookers"
    collected = list(snapshot["participants"]["rows"])
    seen = {repr(collected)}
    total = coverage["total"]
    for _ in range(19):
        if not await move_page(client, frame, grid, "next"):
            break
        fresh = await snapshot_booking_details(
            frame, reference, include_additional=include_additional
        )
        page, state = parse_participants(fresh)
        signature = repr((fresh.get("participants") or {}).get("rows"))
        if page is None or state["total"] != total or signature in seen:
            break
        seen.add(signature)
        collected.extend(fresh["participants"]["rows"])
        snapshot["participants"]["rows"] = collected
        combined, status = parse_participants(snapshot)
        if combined is None or status["complete"]:
            break
    return snapshot


async def read_history(client, frame, reference, details, snapshot):
    from .fbs_paging import move_page

    link = frame.locator(HISTORY_LINK)
    if await link.count() == 0:
        return details
    if await link.count() != 1 or snapshot.get("visible_booking_forms") != 1:
        details["field_status"]["approval_history"] = "ambiguous_scope"
        return details
    try:
        await link.click()
        await frame.locator("#" + HISTORY_GRID + "_content:visible").wait_for(
            timeout=15000
        )
        await client._wait_for_idle(frame)
        await snapshot_booking_details(frame, reference)
        await move_page(client, frame, HISTORY_GRID, "first")
        combined = None
        seen = set()
        for _ in range(20):
            page = parse_history(await frame.evaluate(HISTORY_SNAPSHOT, HISTORY_GRID))
            signature = repr(page["events"])
            if (
                signature in seen
                or combined is not None
                and combined["total"] != page["total"]
            ):
                raise LayoutChanged("FBS workflow history changed during pagination")
            seen.add(signature)
            if combined is None:
                combined = {**page, "events": []}
            combined["events"].extend(page["events"])
            if len(combined["events"]) > combined["total"]:
                raise LayoutChanged("FBS workflow history exceeded its verified total")
            combined["complete"] = len(combined["events"]) == combined["total"]
            if (
                combined["complete"]
                or len(combined["events"]) >= 200
                or not await move_page(client, frame, HISTORY_GRID, "next")
            ):
                break
        await snapshot_booking_details(frame, reference)
        combined["reference_binding"] = "opened_from_verified_booking_view"
        return apply_history(details, combined)
    except (PlaywrightError, LayoutChanged, TimeoutError):
        details["field_status"]["approval_history"] = "unreadable"
        return details
    finally:
        close = frame.locator(HISTORY_CLOSE)
        if await close.count() == 1:
            await close.click()


async def read_open_booking_details(client, record, *, include_additional=True):
    frame, snapshot = await wait_for_details(
        client, record["reference_number"], include_additional=include_additional
    )
    snapshot = await collect_participants(
        client,
        frame,
        record["reference_number"],
        snapshot,
        include_additional=include_additional,
    )
    details = parse_booking_details(
        snapshot, record, include_additional=include_additional
    )
    return await read_history(
        client, frame, record["reference_number"], details, snapshot
    )


async def read_booking_details(
    client, reference, *, source="auto", include_additional=True
):
    from .fbs_account import booking_rows, read_bookings, read_task_list
    from .fbs_tasks import task_rows

    for kind in [source] if source != "auto" else ["bookings", "tasks"]:
        listing = await (
            read_bookings(client, reference=reference)
            if kind == "bookings"
            else read_task_list(client, reference=reference)
        )
        if not any(item["reference_number"] == reference for item in listing[kind]):
            continue
        frame = client.page.frame(name="frameContent")
        rows = await (booking_rows(frame) if kind == "bookings" else task_rows(frame))
        matches = [
            (record, row)
            for record, row in rows
            if record["reference_number"] == reference
        ]
        if len(matches) != 1:
            raise LayoutChanged(
                "The booking reference was not unique in the verified listing"
            )
        record, row = matches[0]
        view = row.locator('a[id$="_BtnView"]:visible')
        if await view.count() != 1 or not await view.is_enabled():
            raise BookingDetailsUnavailable(
                "FBS does not expose a unique read-only VIEW action for this reference"
            )
        await client.progress("reading the requested booking's verified detail view")
        await view.click()
        if kind == "tasks":
            record = {
                "reference_number": record["reference_number"],
                "room": record["room"],
                "date_time": record["date_time"],
                "fbs_status": None,
                "can_cancel": None,
                "task_status": record["status"],
                "requested_by": record["requested_by"],
                "request_type": record["request_type"],
            }
        details = await read_open_booking_details(
            client, record, include_additional=include_additional
        )
        details["source"] = "my_bookings" if kind == "bookings" else "task_list"
        return details
    raise BookingDetailsUnavailable(
        "The reference was not found in the checked booking/task pages. Refresh the listing and inspect coverage."
    )
