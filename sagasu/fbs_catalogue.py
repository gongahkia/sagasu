"""Allowlisted reads from native facility details and profile credit summaries."""

import asyncio
import re
from datetime import datetime
from decimal import Decimal

from .fbs_client import FBS_TIMEZONE, LayoutChanged
from .fbs_http import fbs_document_url
from .observability import observed_step


def labelled_value(text, label):
    if not isinstance(text, str) or len(text) > 8000:
        raise LayoutChanged("The FBS labelled value is unreadable")
    match = re.fullmatch(re.escape(label) + r"\s*:\s*(.*)", text.strip(), re.I | re.S)
    if not match:
        raise LayoutChanged("The FBS field label changed")
    return " ".join(match[1].split())


def parse_room_details(snapshot, expected_room):
    required = {
        "Building": "BUILDING",
        "ResourceType": "FACILITY TYPE",
        "ResourceName": "FACILITY",
        "Capacity": "CAPACITY",
    }
    values = {
        key: labelled_value(snapshot.get(key), label) for key, label in required.items()
    }
    if (
        values["ResourceName"] != expected_room
        or not values["Building"]
        or not values["ResourceType"]
    ):
        raise LayoutChanged(
            "The facility detail identity does not match the requested room"
        )
    if not re.fullmatch(r"\d{1,6}", values["Capacity"]) or int(values["Capacity"]) < 1:
        raise LayoutChanged("The facility capacity could not be verified")
    description = (
        labelled_value(snapshot["Description"], "DESCRIPTION")
        if snapshot.get("Description") is not None
        else None
    )
    hours = snapshot.get("hours")
    parsed_hours = None
    if hours is not None:
        expected_headers = [
            "PERIOD",
            "TYPE",
            "MON",
            "TUE",
            "WED",
            "THU",
            "FRI",
            "SAT",
            "SUN",
            "PH",
        ]
        totals = re.findall(
            r"(\d+)\s+ITEM\(S\)\s+TOTAL", hours.get("summary", ""), re.I
        )
        if hours.get("headers") != expected_headers or len(set(totals)) != 1:
            raise LayoutChanged("Facility operating hours layout changed")
        total = int(totals[0])
        rows = hours.get("rows", [])
        if total == 0 and rows == [["No items"]]:
            rows = []
        if (
            len(rows) != total
            or total > 100
            or any(len(row) != len(expected_headers) for row in rows)
        ):
            raise LayoutChanged("Facility operating hours coverage is incomplete")
        parsed_hours = [
            dict(
                zip(
                    [
                        "period",
                        "type",
                        "mon",
                        "tue",
                        "wed",
                        "thu",
                        "fri",
                        "sat",
                        "sun",
                        "public_holiday",
                    ],
                    row,
                )
            )
            for row in rows
        ]
    equipment = snapshot.get("equipment")
    if equipment is not None and (
        not isinstance(equipment, str) or len(equipment) > 8000
    ):
        raise LayoutChanged("Facility equipment details are unreadable")
    equipment = equipment.strip() or None if equipment is not None else None
    return {
        "room": values["ResourceName"],
        "building": values["Building"],
        "facility_type": values["ResourceType"],
        "capacity": int(values["Capacity"]),
        "description": description or None,
        "operating_hours": parsed_hours,
        "operating_hours_note": "FBS period rules are reported as displayed; current term dates and booking eligibility are not inferred.",
        "equipment_details": equipment,
        "equipment_coverage": "displayed_text_only" if equipment else "not_exposed",
        "accessibility": None,
        "photo_available": snapshot.get("photo_available") is True,
        "photo_note": "FBS-supplied images may illustrate the facility type rather than the exact room.",
        "field_status": {
            "description": "available" if description else "not_exposed",
            "operating_hours": (
                "available" if parsed_hours is not None else "not_exposed"
            ),
            "equipment": "displayed_text_only" if equipment else "not_exposed",
            "accessibility": "not_exposed",
        },
        "source": "fbs_facility_details",
        "scraped_at": datetime.now(FBS_TIMEZONE).isoformat(),
    }


@observed_step("facility.details")
async def read_room_details(client, room, *, include_image=False):
    page, frame = await client.ensure_authenticated()
    for control in (
        "DropMultiBuildingList_c1",
        "DropMultiFloorList_c1",
        "DropMultiFacilityTypeList_c1",
        "DropMultiEquipmentList_c1",
    ):
        if (await frame.locator(f"#{control}_textValue").first.input_value()).strip():
            await client._set_multi_select(frame, control, ())
    await frame.locator("select#DropCapacity_c1").select_option(index=0)
    await client._wait_for_idle(frame)
    await client._set_facility_keyword(frame, room)
    await frame.locator("table#GridResults_gv").wait_for(state="visible", timeout=30000)
    links = frame.locator("table#GridResults_gv").get_by_role(
        "link", name=room, exact=True
    )
    if await links.count() != 1:
        raise LayoutChanged(
            "The exact facility detail link is unavailable or ambiguous"
        )
    await links.click()
    detail = await client._wait_for_frame("FrameEdit")
    await detail.locator("#ResourceName").wait_for(state="visible", timeout=30000)
    await client._wait_for_idle(detail)
    if not fbs_document_url(detail.url):
        raise LayoutChanged("Facility details left the trusted FBS origin")
    snapshot = await detail.evaluate(
        """() => {
        const text = id => document.getElementById(id)?.innerText ?? null;
        const hours = document.getElementById('GridOperatingHours');
        const photo = document.getElementById('Images1_ImageSlide_ImageDisplay');
        return {
            Building:text('Building'), ResourceType:text('ResourceType'), ResourceName:text('ResourceName'),
            Capacity:text('Capacity'), Description:text('Description'), equipment:text('PanelBuiltInEquipment'),
            hours:hours ? {headers:[...hours.querySelectorAll('th')].map(e=>e.innerText.trim()),
                rows:[...hours.querySelectorAll('#GridOperatingHours_content tr.row')].map(e=>[...e.cells].map(e=>e.innerText.trim())),summary:hours.innerText} : null,
            photo_available:!!(photo && photo.getBoundingClientRect().height && photo.complete && photo.naturalWidth && photo.getAttribute('src') && new URL(photo.src,location.href).origin===location.origin)
        };
    }"""
    )
    result = parse_room_details(snapshot, room)
    if include_image:
        from .fbs_account import wait_for_receipt_ready

        await wait_for_receipt_ready(client, detail)
        if await detail.locator(
            'input[type="password"],input[autocomplete="one-time-code"],#idRichContext_DisplaySign,.displaySign'
        ).count() or any(
            not fbs_document_url(child.url) for child in detail.child_frames
        ):
            raise LayoutChanged(
                "The facility image contains an unexpected authentication or external frame"
            )
        result["receipt_png"] = await detail.locator("body").screenshot(
            type="png",
            animations="disabled",
            mask=[detail.locator("input,textarea,select,[contenteditable]")],
            mask_color="#000000",
        )
        result["receipt_kind"] = "facility_details"
    return result


def parse_credits(snapshot):
    result = {}
    for key, label in (
        ("year_start_credits", "TOTAL AVAILABLE CREDITS AT START OF YEAR"),
        ("available_credits", "TOTAL AVAILABLE CREDITS TO USE"),
    ):
        value = labelled_value(snapshot.get(key), label)
        if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}", value):
            raise LayoutChanged("FBS credit amount could not be verified")
        result[key] = str(Decimal(value.replace(",", "")))
    return {
        **result,
        "unit": "FBS credits",
        "source": "fbs_profile_credit_ledger",
        "quota_limits": None,
        "quota_limits_status": "not_exposed_in_credit_summary",
        "booking_cost_lookup": "booking_details.additional_details.credits_deducted",
        "scraped_at": datetime.now(FBS_TIMEZONE).isoformat(),
    }


@observed_step("account.credits")
async def read_credit_status(client):
    page, _ = await client.ensure_authenticated()
    await page.locator("#menu_userprofilemenu").hover()
    await page.locator("#menu_linkEditProfile").click()
    async with asyncio.timeout(30):
        while True:
            frame = page.frame(name="frameBottom")
            if frame and await frame.locator("#LabelBookingCredits").is_visible():
                break
            await page.wait_for_timeout(200)
    await client._wait_for_idle(frame)
    if not fbs_document_url(frame.url):
        raise LayoutChanged("The credit profile left the trusted FBS origin")
    return parse_credits(
        {
            "year_start_credits": await frame.locator(
                "#LabelCreditsAtYearStart"
            ).inner_text(),
            "available_credits": await frame.locator(
                "#LabelBookingCredits"
            ).inner_text(),
        }
    )
