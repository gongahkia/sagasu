"""Read current booking classifications and estimated credits without submitting."""

from dataclasses import asdict
from decimal import Decimal
import re

from .fbs_client import (
    BookingUnavailable,
    FBS_URL,
    LayoutChanged,
    require_future_booking,
)
from .fbs_http import fbs_document_url


def validate_usage(usage_type, booking_usage):
    if any(
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= 100
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        for value in (usage_type, booking_usage)
    ):
        raise ValueError(
            "Use exact single-line FBS usage labels of 1–100 characters; booking_options reads current choices."
        )
    return usage_type.strip(), booking_usage.strip()


def parse_options(snapshot):
    result = {}
    for key in ("usage_types", "booking_usages"):
        choices = snapshot.get(key)
        if (
            not isinstance(choices, list)
            or not choices
            or len(choices) > 100
            or any(
                not isinstance(label, str) or not label.strip() or len(label) > 100
                for label in choices
            )
            or len(set(choices)) != len(choices)
        ):
            raise LayoutChanged("Current FBS booking choices could not be verified")
        result[key] = choices
    value = snapshot.get("estimated_credits")
    if value is not None:
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}", value.strip()
        ):
            raise LayoutChanged("The booking credit estimate could not be verified")
        value = str(Decimal(value.strip().replace(",", "")))
    result["estimated_credits"] = value
    for key, choices in (
        ("selected_usage_type", "usage_types"),
        ("selected_booking_usage", "booking_usages"),
    ):
        selected = snapshot.get(key)
        if selected and selected not in result[choices]:
            raise LayoutChanged("Selected FBS booking classification changed")
        result[key] = selected or None
    return result


async def read_booking_options(client, search, room):
    require_future_booking(search)
    snapshot = await client.search(search, room_name=room)
    if not any(
        item.name == room and item.is_free(search.start_time, search.end_time)
        for item in snapshot.rooms
    ):
        raise BookingUnavailable(
            "A currently free interval is required to read native booking options"
        )
    try:
        form = await client._prepare_booking_frame(
            client.page.frame(name="frameContent"), search, room
        )
        if not fbs_document_url(form.url):
            raise LayoutChanged("Booking options left the trusted FBS origin")
        await client._wait_for_idle(form)
        result = parse_options(
            await form.evaluate(
                """() => {
            const type=document.getElementById('bookingFormControl1_DropDownUsageType_c1');
            const usage=document.getElementById('bookingFormControl1_DropDownSpaceBookingUsage_c1');
            const labels=e=>e?[...e.options].filter(o=>o.value&&!o.disabled).map(o=>o.text.trim()):null;
            const selected=e=>e&&e.value?e.selectedOptions[0].text.trim():null;
            return {usage_types:labels(type),booking_usages:labels(usage),selected_usage_type:selected(type),selected_booking_usage:selected(usage),estimated_credits:document.getElementById('bookingFormControl1_BookingCreditsDeducted_Label_c1')?.innerText.trim()??null};
        }"""
            )
        )
        return {
            **result,
            "room": room,
            "config": asdict(search),
            "source": "fbs_unsent_booking_form",
            "credit_estimate_note": "Credits shown for the current form selections; final costs and permissions may change when classifications or availability change.",
            "submitted": False,
        }
    except Exception as error:
        from .operation_diagnostics import capture_failure

        await capture_failure(client, error)
        raise
    finally:
        await client.page.goto(FBS_URL, wait_until="domcontentloaded", timeout=15000)
