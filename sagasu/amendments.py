"""Verified native EDIT flows; a room change never becomes an implicit rebooking."""

import asyncio
import copy
import hmac
import json
import re
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .action_journal import checkpoint_verified
from .alternatives import result_from_dict
from .booking_details import booking_window
from .booking_options import parse_options
from .fbs_account import (
    booking_rows,
    read_bookings,
    capture_booking_evidence,
    wait_for_receipt_ready,
)
from .fbs_client import (
    AuthenticationRequired,
    BookingOutcomeUnknown,
    BookingUnavailable,
    FBSClient,
    FBS_TIMEZONE,
    FBS_URL,
    LayoutChanged,
    SearchConfig,
    require_future_booking,
    time_to_minutes,
    minutes_to_time,
)
from .fbs_http import fbs_document_url
from .repeat import normalize_date
from .room_plans import _free_intervals, _covers

FIELDS = ("date", "start_time", "end_time", "purpose", "usage_type", "booking_usage")
EDIT_PATH = "/modules/bigspace/bookingmanagement/edit.aspx"
CONFIRM_TIMEOUT_MS = 45000


class AmendmentUnavailable(BookingUnavailable):
    default_reason = "amendment_unavailable"


class AmendmentOutcomeUnknown(BookingOutcomeUnknown):
    pass


def canonical(key, value):
    if not isinstance(value, str):
        return value
    value = " ".join(value.split())
    return value.casefold() if key in {"usage_type", "booking_usage"} else value


def verification_digest(key, values):
    if (
        not isinstance(key, bytes)
        or len(key) != 32
        or not isinstance(values, dict)
        or not values
        or set(values) - set(FIELDS)
        or any(not isinstance(value, str) for value in values.values())
    ):
        raise ValueError("Invalid amendment comparison data")
    data = {field: canonical(field, value) for field, value in values.items()}
    return hmac.digest(
        key, json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), "sha256"
    ).hex()


def matches_digest(key, summary, observed):
    fields = summary.get("verification_fields", "").split(",")
    expected = summary.get("verification_digest")
    if (
        not fields
        or set(fields) - set(FIELDS)
        or not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or len(set(fields)) != len(fields)
        or any(not isinstance(observed.get(field), str) for field in fields)
    ):
        return False
    return hmac.compare_digest(
        expected, verification_digest(key, {field: observed[field] for field in fields})
    )


def require_target(payload, *, now=None):
    expected, target, changes = (
        payload["expected"],
        payload["target"],
        payload["changes"],
    )
    config = SearchConfig(target["date"], target["start_time"], target["end_time"])
    if any(field in changes for field in ("date", "start_time")):
        try:
            require_future_booking(config, now=now)
        except BookingUnavailable as error:
            raise AmendmentUnavailable(
                "Changed booking starts must remain in the future"
            ) from error
    elif "end_time" in changes:
        end = datetime.strptime(
            target["date"] + " " + target["end_time"], "%d-%b-%Y %H:%M"
        ).replace(tzinfo=FBS_TIMEZONE)
        if end <= (now or datetime.now(FBS_TIMEZONE)):
            raise AmendmentUnavailable(
                "The amended end time must still be in the future"
            )
    return config


def prepare_amendment(options, changes, key, *, now=None):
    if options.get("available") is not True:
        raise ValueError(
            "FBS must expose a verified native EDIT form for this booking first."
        )
    if not isinstance(changes, dict) or not changes or set(changes) - set(FIELDS):
        raise ValueError(
            "Change date, start_time, end_time, purpose, usage_type or booking_usage. Native room/participant changes are not implemented; no cancellation/rebooking is implicit."
        )
    expected = copy.deepcopy(options["current"])
    target = copy.deepcopy(expected)
    for field, value in changes.items():
        if (
            field not in options["supported_changes"]
            or not isinstance(value, str)
            or not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError(
                "Choose an editable field and an explicit single-line replacement value."
            )
        if field == "date":
            value = normalize_date(value)
        elif field == "purpose":
            if len(value.strip()) > 200:
                raise ValueError("The amended purpose must be 1–200 characters.")
            value = value.strip()
        elif field in {"usage_type", "booking_usage"}:
            choices = options[
                "usage_types" if field == "usage_type" else "booking_usages"
            ]
            if value not in choices:
                raise ValueError(
                    "Choose an exact classification label from amendment_options."
                )
        target[field] = value
    changed = {
        field: target[field]
        for field in changes
        if canonical(field, target[field]) != canonical(field, expected[field])
    }
    if not changed:
        raise ValueError(
            "The requested displayed values are unchanged; no amendment review was prepared."
        )
    payload = {"expected": expected, "target": target, "changes": changed}
    require_target(payload, now=now)
    payload["verification_fields"] = ",".join(sorted(changed))
    payload["verification_digest"] = verification_digest(key, changed)
    return payload


async def open_edit(client, reference):
    listing = await read_bookings(client)
    frame = client.page.frame(name="frameContent")
    keyword = frame.locator("input#txtSearch_c1")
    if await keyword.count() == 1 and (await keyword.input_value()).strip():
        raise AmendmentUnavailable(
            "Clear the native My Bookings keyword filter before preparing amendment options; filtered rows cannot establish unique identity"
        )
    selected = [
        row for row in listing["bookings"] if row["reference_number"] == reference
    ]
    if not listing["complete"] or len(selected) != 1:
        raise AmendmentUnavailable(
            "Complete My Bookings coverage and one exact reference are required for native editing"
        )
    expected = selected[0]
    window = booking_window(expected)
    same_identity = []
    for record in listing["bookings"]:
        if (
            record["room"] == expected["room"]
            and record["fbs_status"] == expected["fbs_status"]
        ):
            try:
                if booking_window(record) == window:
                    same_identity.append(record)
            except ValueError:
                raise AmendmentUnavailable(
                    "A competing booking identity could not be read"
                ) from None
    if len(same_identity) != 1:
        raise AmendmentUnavailable(
            "The native edit form omits references; this booking's room/time/status identity is ambiguous"
        )
    frame = client.page.frame(name="frameContent")
    if not any(
        record["reference_number"] == reference
        for record, _ in await booking_rows(frame)
    ):
        await read_bookings(client, reference=reference)
        frame = client.page.frame(name="frameContent")
    matches = [
        (record, row)
        for record, row in await booking_rows(frame)
        if record["reference_number"] == reference
    ]
    if len(matches) != 1:
        raise AmendmentUnavailable("The exact booking could not be uniquely found")
    record, row = matches[0]
    if any(
        record[field] != expected[field]
        for field in ("room", "date_time", "fbs_status")
    ):
        raise AmendmentUnavailable(
            "The source booking changed before its EDIT action could be opened"
        )
    edit = row.locator('a[id$="_BtnEdit"]:visible')
    if await edit.count() != 1:
        return None, record
    await client.progress("opening the exact booking's native EDIT form")
    await edit.click()
    async with asyncio.timeout(30):
        while True:
            frame = client.page.frame(name="frameContent")
            if (
                frame
                and fbs_document_url(frame.url)
                and urlsplit(frame.url).path.casefold() == EDIT_PATH
                and await frame.locator(
                    "#bookingFormControl1_TextboxPurpose_c1"
                ).is_editable()
            ):
                await wait_for_receipt_ready(client, frame)
                record = {**record, "_edit_source_verified": True}
                return frame, record
            await client.page.wait_for_timeout(200)


async def edit_snapshot(frame, record, *, verify_original=True):
    if (
        record.get("_edit_source_verified") is not True
        or not fbs_document_url(frame.url)
        or urlsplit(frame.url).path.casefold() != EDIT_PATH
    ):
        raise LayoutChanged(
            "The edit form was not opened through a verified native booking action"
        )
    room_field = frame.locator("#bookingFormControl1_LabelFacilityName_c1")
    if await room_field.count() != 1 or not await room_field.is_visible():
        raise LayoutChanged("The native edit facility is unreadable")
    room = " ".join((await room_field.inner_text()).split())
    if room != record["room"]:
        raise LayoutChanged("The native EDIT form shows a different facility")
    labels = {}
    for key, control in (
        ("building", "LabelBuilding"),
        ("facility_type", "LabelPassType"),
    ):
        label = frame.locator(f"#bookingFormControl1_{control}_c1")
        labels[key] = (
            (await label.inner_text()).strip()
            if await label.count() == 1 and await label.is_visible()
            else None
        )
    data = await frame.evaluate(
        """prefix => {
        const el = suffix => document.getElementById(prefix+suffix);
        const value = suffix => el(suffix)?.value ?? null;
        const selected = suffix => {const e=el(suffix);return e&&e.value?e.selectedOptions[0].text.trim():null;};
        const choices = suffix => {const e=el(suffix);return e?[...e.options].filter(o=>o.value&&!o.disabled).map(o=>o.text.trim()):null;};
        const editable = suffix => {const e=el(suffix);return !!(e&&e.getClientRects().length&&!e.disabled&&!e.readOnly);};
        const grid=el('GridCoBookers');
        return {date:value('BookingDate_c1_textDate'),start:value('DropDownStartTime_c1'),end:value('DropDownEndTime_c1'),purpose:value('TextboxPurpose_c1'),
            usage_types:choices('DropDownUsageType_c1'),booking_usages:choices('DropDownSpaceBookingUsage_c1'),selected_usage_type:selected('DropDownUsageType_c1'),selected_booking_usage:selected('DropDownSpaceBookingUsage_c1'),estimated_credits:el('BookingCreditsDeducted_Label_c1')?.innerText.trim()??null,
            editable:{date:!!(el('BookingDate_c1_textDate')?.getClientRects().length&&!el('BookingDate_c1_textDate').disabled),start_time:editable('DropDownStartTime_c1'),end_time:editable('DropDownEndTime_c1'),purpose:editable('TextboxPurpose_c1'),usage_type:editable('DropDownUsageType_c1'),booking_usage:editable('DropDownSpaceBookingUsage_c1')},
            participants:grid?{headers:[...grid.querySelectorAll('th')].map(e=>e.innerText.trim()),rows:[...grid.querySelectorAll('tr.row')].map(e=>[...e.cells].map(e=>e.innerText.trim())),summary:grid.innerText}:null};
    }""",
        "bookingFormControl1_",
    )
    date = normalize_date(data["date"])
    start, end = [
        datetime.strptime(data[key], "%m/%d/%Y %I:%M:%S %p") for key in ("start", "end")
    ]
    if any(value.strftime("%d-%b-%Y") != date for value in (start, end)):
        raise LayoutChanged("The native amendment dates disagree")
    window = {
        "date": date,
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
    }
    SearchConfig(**window)
    if verify_original and window != booking_window(record):
        raise AmendmentUnavailable(
            "The native booking time changed; refresh its amendment options"
        )
    people = data.get("participants")
    if not isinstance(people, dict) or [
        h.casefold() for h in people["headers"] if h
    ] != ["name"]:
        raise LayoutChanged("The current participant grid could not be verified")
    totals = re.findall(r"(\d+)\s+ITEM\(S\)\s+TOTAL", people["summary"], re.I)
    if len(set(totals)) != 1:
        raise LayoutChanged("Participant coverage is unknown")
    rows = people["rows"]
    if int(totals[0]) == 0 and rows == [["No items"]]:
        rows = []
    if len(rows) != int(totals[0]) or any(
        len(row) != 2 or not row[1].strip() for row in rows
    ):
        raise LayoutChanged("The amendment participant grid is incomplete")
    choices = parse_options(data)
    purpose = data["purpose"]
    if not isinstance(purpose, str) or len(purpose) > 8192:
        raise LayoutChanged("The current booking purpose is unreadable")
    current = {
        "reference_number": record["reference_number"],
        "room": room,
        **window,
        "purpose": purpose,
        "usage_type": choices["selected_usage_type"],
        "booking_usage": choices["selected_booking_usage"],
        "participants": [row[1] for row in rows],
        **labels,
        "fbs_status": record["fbs_status"],
    }
    return {
        "available": True,
        "native_edit_offered": True,
        "current": current,
        **choices,
        "supported_changes": [key for key in FIELDS if data["editable"][key]],
        "room_change_supported": False,
        "participant_changes_supported": False,
        "source": "fbs_native_edit",
        "reference_binding": "Exact My Bookings EDIT action from a complete listing with unique room/time/status identity. The edit form's room/date/times match that source; FBS EDIT omits the reference number.",
        "submitted": False,
    }


async def read_amendment_options(client, reference):
    opened = False
    try:
        frame, record = await open_edit(client, reference)
        if frame is None:
            return {
                "available": False,
                "native_edit_offered": False,
                "reference_number": reference,
                "fbs_status": record["fbs_status"],
                "supported_changes": [],
                "source": "fbs_my_bookings_actions",
                "submitted": False,
            }
        opened = True
        return await edit_snapshot(frame, record)
    except Exception as error:
        from .operation_diagnostics import capture_failure

        await capture_failure(client, error)
        raise
    finally:
        if opened:
            await client.page.goto(
                FBS_URL, wait_until="domcontentloaded", timeout=15000
            )


def additional_intervals(payload):
    old, new = payload["expected"], payload["target"]
    a, b, c, d = [
        time_to_minutes(value)
        for value in (
            old["start_time"],
            old["end_time"],
            new["start_time"],
            new["end_time"],
        )
    ]
    intervals = (
        [(c, d)]
        if old["date"] != new["date"]
        else [
            (start, end)
            for start, end in ((c, min(d, a)), (max(c, b), d))
            if start < end
        ]
    )
    if intervals and not old.get("building"):
        raise AmendmentUnavailable("Added intervals require a verified building scope")
    return [
        SearchConfig(
            new["date"],
            minutes_to_time(start),
            minutes_to_time(end),
            buildings=(old["building"],),
            facility_types=(old["facility_type"],) if old.get("facility_type") else (),
        )
        for start, end in intervals
    ]


async def check_added_intervals(payload, search):
    try:
        for config in additional_intervals(payload):
            require_future_booking(config)
            snapshot = result_from_dict(await search(config))
            rooms = _free_intervals(snapshot, config, set())
            if not _covers(
                rooms.get(payload["expected"]["room"], []),
                time_to_minutes(config.start_time),
                time_to_minutes(config.end_time),
            ):
                raise AmendmentUnavailable(
                    "The added/rescheduled interval is not verified free in the requested room"
                )
    except (AuthenticationRequired, BookingUnavailable):
        raise
    except Exception as error:
        raise AmendmentUnavailable(
            "Availability for the added/rescheduled interval could not be verified; this is not an empty search result"
        ) from error


async def set_edit_date(client, frame, date):
    field = frame.locator("#bookingFormControl1_BookingDate_c1_textDate")
    if normalize_date(await field.input_value()) == date:
        return
    target = datetime.strptime(date, "%d-%b-%Y")
    await field.click()
    await frame.locator("#__calendar_daysTable").wait_for(
        state="visible", timeout=10000
    )
    for _ in range(25):
        title = (await frame.locator("#__calendar_title").inner_text()).strip()
        current = datetime.strptime(title, "%B, %Y")
        months = (target.year - current.year) * 12 + target.month - current.month
        if not months:
            day = frame.locator(f'#__calendar_daysTable div.day[title="{date}"]')
            if (
                await day.count() != 1
                or not await day.is_visible()
                or await day.get_attribute("aria-disabled") == "true"
                or re.search(
                    r"disabled|unselectable",
                    await day.get_attribute("class") or "",
                    re.I,
                )
            ):
                raise AmendmentUnavailable(
                    "The target day is not selectable in the native calendar"
                )
            await day.click()
            await client._wait_for_idle(frame)
            if normalize_date(await field.input_value()) != date:
                raise AmendmentUnavailable("FBS did not retain the amended date")
            return
        if abs(months) > 24:
            break
        await frame.locator(
            "#__calendar_nextArrow" if months > 0 else "#__calendar_prevArrow"
        ).click()
        await frame.wait_for_function(
            "previous => document.getElementById('__calendar_title')?.innerText.trim() !== previous",
            arg=title,
            timeout=5000,
        )
    raise AmendmentUnavailable("The native date-picker navigation limit was reached")


async def select_edit_time(client, frame, suffix, date, time):
    field = frame.locator(f"#bookingFormControl1_DropDown{suffix}Time_c1")
    expected = datetime.strptime(date + " " + time, "%d-%b-%Y %H:%M")
    values = await field.locator("option").evaluate_all(
        "es=>es.filter(e=>!e.disabled).map(e=>e.value)"
    )
    matches = []
    for value in values:
        try:
            if datetime.strptime(value, "%m/%d/%Y %I:%M:%S %p") == expected:
                matches.append(value)
        except ValueError:
            continue
    if len(matches) != 1:
        raise AmendmentUnavailable(
            "The amended date/time is not an exact selectable FBS option"
        )
    if await field.input_value() != matches[0]:
        await field.select_option(value=matches[0])
        await client._wait_for_idle(frame)


def same_original(current, expected):
    keys = (
        "reference_number",
        "room",
        "date",
        "start_time",
        "end_time",
        "purpose",
        "usage_type",
        "booking_usage",
        "fbs_status",
    )
    return all(
        canonical(key, current.get(key)) == canonical(key, expected.get(key))
        for key in keys
    ) and Counter(current["participants"]) == Counter(expected["participants"])


async def prepare_native_amendment(client, payload):
    require_target(payload)
    frame, record = await open_edit(client, payload["expected"]["reference_number"])
    if frame is None:
        raise AmendmentUnavailable("FBS no longer offers EDIT for this booking")
    options = await edit_snapshot(frame, record)
    if not same_original(options["current"], payload["expected"]):
        raise AmendmentUnavailable(
            "The booking changed after review; read new amendment_options"
        )
    target = payload["target"]
    await set_edit_date(client, frame, target["date"])
    await select_edit_time(client, frame, "Start", target["date"], target["start_time"])
    await select_edit_time(client, frame, "End", target["date"], target["end_time"])
    if "purpose" in payload["changes"]:
        await frame.locator("#bookingFormControl1_TextboxPurpose_c1").fill(
            target["purpose"]
        )
    for key, control in (
        ("usage_type", "DropDownUsageType_c1"),
        ("booking_usage", "DropDownSpaceBookingUsage_c1"),
    ):
        if key in payload["changes"]:
            await frame.locator("#bookingFormControl1_" + control).select_option(
                label=target[key]
            )
            await client._wait_for_idle(frame)
    terms = frame.locator("#bookingFormControl1_TermsAndConditionsCheckbox_c1")
    if await terms.count() == 1 and await terms.is_enabled():
        await terms.check()
    adapter = SimpleNamespace(
        search=SearchConfig(target["date"], target["start_time"], target["end_time"]),
        room=target["room"],
        purpose=target["purpose"],
        usage_type=target["usage_type"] or "",
        booking_usage=target["booking_usage"] or "",
    )
    await client._verify_booking_details(frame, adapter)
    prepared = await edit_snapshot(frame, record, verify_original=False)
    if any(
        canonical(key, prepared["current"].get(key)) != canonical(key, target.get(key))
        for key in FIELDS
    ):
        raise AmendmentUnavailable(
            "The native edit form did not retain all target fields"
        )
    if Counter(prepared["current"]["participants"]) != Counter(
        payload["expected"]["participants"]
    ):
        raise AmendmentUnavailable(
            "The native amendment did not retain the existing participants"
        )
    require_target(payload)
    return frame, prepared


def observed_values(details):
    additional = details.get("additional_details") or {}
    return {
        **{
            key: details.get(key)
            for key in ("date", "start_time", "end_time", "purpose")
        },
        "usage_type": additional.get("use_type"),
        "booking_usage": additional.get("booking_usage"),
    }


def verify_amended(details, payload):
    target, expected = payload["target"], payload["expected"]
    values = observed_values(details)
    return (
        details.get("reference_verified") is True
        and details.get("reference_number") == expected["reference_number"]
        and details.get("room") == expected["room"]
        and all(
            values[key] == target[key] for key in ("date", "start_time", "end_time")
        )
        and all(
            canonical(key, values.get(key)) == canonical(key, value)
            for key, value in payload["changes"].items()
        )
        and (
            details.get("fbs_status", "").casefold() == "confirmed"
            or details.get("fbs_status", "").casefold().startswith("pending")
        )
    )


async def execute_amendment(
    payload, *, submit=False, progress, auth_notify, interactive=True
):
    from .booking_details import read_booking_details

    attempted = False
    async with FBSClient(
        progress=progress, auth_notify=auth_notify, interactive=interactive
    ) as client:
        try:
            frame, prepared = await prepare_native_amendment(client, payload)
            result = {
                "status": "ready",
                "reference_number": payload["expected"]["reference_number"],
                "room": payload["expected"]["room"],
                "changes": copy.deepcopy(payload["changes"]),
                "estimated_credits": prepared["estimated_credits"],
                "participant_count": len(prepared["current"]["participants"]),
                "participant_verification": "Unchanged native displayed name rows; participant controls were not modified.",
                "submitted": False,
            }
            if not submit:
                return result
            confirm = frame.locator("a#panel_UIButton2:visible")
            if (
                await confirm.count() != 1
                or (await confirm.inner_text()).strip().casefold() != "confirm"
            ):
                raise AmendmentUnavailable(
                    "The native amendment CONFIRM action is unavailable"
                )
            attempted = True
            async with client.page.expect_response(
                lambda response: response.request.method == "POST"
                and response.frame is frame
                and urlsplit(response.url).path.casefold() == EDIT_PATH,
                timeout=CONFIRM_TIMEOUT_MS,
            ) as pending:
                await confirm.click()
            await (await pending.value).finished()
            current_frame = client.page.frame(name="frameContent")
            if current_frame:
                await client._wait_for_idle(current_frame)
            details = await read_booking_details(
                client,
                result["reference_number"],
                source="bookings",
                include_additional=True,
            )
            if not verify_amended(details, payload):
                raise AmendmentOutcomeUnknown(
                    "The exact reference's amended fields could not be verified"
                )
            result.update(
                status="verified_amendment",
                fbs_status=details["fbs_status"],
                submitted=True,
            )
            checkpoint_verified(result)
            try:
                result["receipt_png"] = await capture_booking_evidence(
                    client, result["reference_number"]
                )
                result["receipt_kind"] = "booking_amendment"
            except Exception:
                result["receipt_error"] = (
                    "Amendment verified; screenshot unavailable. Do not submit another amendment."
                )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            from .operation_diagnostics import capture_failure

            await capture_failure(client, error)
            if attempted:
                raise AmendmentOutcomeUnknown(
                    "Native amendment confirmation was attempted but its outcome could not be verified; reconcile the exact reference before another amendment"
                ) from error
            if isinstance(error, (AuthenticationRequired, BookingUnavailable)):
                raise
            raise AmendmentUnavailable(
                "The native amendment could not be prepared; no CONFIRM action was attempted"
            ) from error
        finally:
            if not attempted and client.page and fbs_document_url(client.page.url):
                await client.page.goto(
                    FBS_URL, wait_until="domcontentloaded", timeout=15000
                )
