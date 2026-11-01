"""Guided booking questions and per-user saved co-bookers.

Only plain data enters Telegram persistence. This module prepares drafts; the
existing booking confirmation handler alone calls FBS.
"""

import re
import secrets
import time
from html import escape

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from .fbs_client import (
    BookingRequest,
    BookingUnavailable,
    SearchConfig,
    require_future_booking,
)

SMU_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@(smu\.edu\.sg|(scis|sis|law|business|accountancy|economics|socsc)\.smu\.edu\.sg)$",
    re.IGNORECASE,
)
FLOW_KEY = "booking_wizard"
CONTACTS_KEY = "saved_cobookers"
PAGE_SIZE = 8


def abandon_setup(context):
    context.user_data.pop(FLOW_KEY, None)


async def validate_booking_time(message, context, config):
    try:
        require_future_booking(config)
    except BookingUnavailable as error:
        for key in (
            FLOW_KEY,
            "booking_drafts",
            "selected_room",
            "last_search",
            "last_room_order",
        ):
            context.user_data.pop(key, None)
        await message.reply_text(
            f"⚠️ {error}",
            reply_markup=Markup(
                [[Button("⚙️ Change date/time", callback_data="open_config")]]
            ),
        )
        return False
    return True


def contacts(context):
    return context.user_data.setdefault(CONTACTS_KEY, {})


def keyboard(flow, rows):
    return Markup(
        [
            [
                Button(label, callback_data=f"wizard:{flow['id']}:{action}")
                for label, action in row
            ]
            for row in rows
        ]
    )


def new_flow(message, context, step, **values):
    flow = {
        "id": secrets.token_urlsafe(6),
        "chat_id": message.chat_id,
        "expires_at": time.time() + 900,
        "step": step,
        **values,
    }
    context.user_data[FLOW_KEY] = flow
    return flow


def current_flow(message, context, token=None):
    flow = context.user_data.get(FLOW_KEY)
    if not flow or flow["chat_id"] != message.chat_id:
        return None
    if flow["expires_at"] < time.time():
        abandon_setup(context)
        return None
    if token is not None and token != flow["id"]:
        return None
    return flow


async def begin_booking(message, context, purpose=None):
    room = context.user_data.get("selected_room")
    search = context.user_data.get("last_search") or {}
    if not room or not search.get("config"):
        await message.reply_text("Search for rooms and choose one first with /start.")
        return
    if not await validate_booking_time(
        message, context, SearchConfig.from_mapping(search["config"])
    ):
        return
    if getattr(context, "ui_panel", None):
        context.ui_panel.back_to("ui:results:0")
    # Each new setup supersedes its previous pending review.
    context.user_data.pop("booking_drafts", None)
    flow = new_flow(
        message, context, "purpose", room=room, search=dict(search["config"])
    )
    if purpose:
        await accept_text(message, context, purpose)
    else:
        await prompt(message, context, flow)


async def open_contacts(message, context):
    flow = new_flow(message, context, "contacts", page=0)
    await prompt(message, context, flow)


async def cobookers_command(update, context):
    await open_contacts(update.effective_message, context)


async def prompt(message, context, flow, *, notice=None):
    if getattr(context, "ui_panel", None):
        context.ui_panel.back_to("ui:results:0" if "search" in flow else "ui:home")
    step = flow["step"]
    rows = []
    if step == "purpose":
        cfg = flow["search"]
        text = (
            f"<b>Book {escape(flow['room'])}</b>\n"
            f"{escape(cfg['date'])}, {escape(cfg['start_time'])}–{escape(cfg['end_time'])}\n\n"
            "What is this booking for? Send a short purpose, such as ‘Project meeting’."
        )
        if flow.get("purpose"):
            text += f"\nPrevious purpose: {escape(flow['purpose'])}"
    elif step == "co_booker":
        saved = list(contacts(context).items())
        text = "<b>Who will co-book with you?</b>\nChoose a saved co-booker or send their full SMU email address."
        page = max(0, min(flow.get("page", 0), max(0, (len(saved) - 1) // PAGE_SIZE)))
        flow["page"] = page
        for key, person in saved[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
            rows.append([(f"👤 {person['name']} · {person['email']}", f"use:{key}")])
        rows.extend(page_buttons(page, len(saved)))
        rows.extend(
            [[("＋ Enter another co-booker", "new")], [("← Change purpose", "back")]]
        )
    elif step == "co_email":
        text = "What is your co-booker’s full SMU email address?\nFor example: friend@smu.edu.sg"
        rows.append([("← Saved co-bookers", "back")])
    elif step == "save_choice":
        text = f"Save <code>{escape(flow['co_booker'])}</code> for future bookings?"
        rows.extend(
            [
                [("💾 Save with a name", "save"), ("Use once", "skip")],
                [("← Choose someone else", "back")],
            ]
        )
    elif step == "save_name":
        text = "What name should I save this co-booker under?"
        rows.append([("← Back", "back")])
    elif step == "contact_name":
        text = "What is the co-booker’s name?"
        rows.append([("← Saved co-bookers", "back")])
    elif step == "contact_email":
        text = f"What is {escape(flow['name'])}’s full SMU email address?"
        rows.append([("← Change name", "back")])
    elif step == "contacts":
        saved = list(contacts(context).items())
        page = max(0, min(flow.get("page", 0), max(0, (len(saved) - 1) // PAGE_SIZE)))
        flow["page"] = page
        text = "<b>Saved co-bookers</b>\nChoose them during booking instead of typing their email again."
        if not saved:
            text += "\n\nYou haven’t saved anyone yet."
        for key, person in saved[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
            text += f"\n\n<b>{escape(person['name'])}</b>\n<code>{escape(person['email'])}</code>"
            rows.append(
                [(f"Remove {person['name']} · {person['email']}", f"remove:{key}")]
            )
        rows.extend(page_buttons(page, len(saved)))
        rows.append([("＋ Add co-booker", "add"), ("Done", "done")])
    else:
        return
    if step != "contacts":
        rows.append([("Cancel", "cancel")])
    if notice:
        text = f"⚠️ {escape(notice)}\n\n{text}"
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard(flow, rows))


def page_buttons(page, count):
    row = []
    if page > 0:
        row.append(("← Previous", f"page:{page - 1}"))
    if (page + 1) * PAGE_SIZE < count:
        row.append(("Next →", f"page:{page + 1}"))
    return [row] if row else []


def save_contact(context, name, email):
    saved = contacts(context)
    key = next(
        (
            key
            for key, person in saved.items()
            if person["email"].casefold() == email.casefold()
        ),
        None,
    )
    key = key or secrets.token_urlsafe(6)
    saved[key] = {"name": name, "email": email.casefold()}


async def accept_text(message, context, text):
    flow = current_flow(message, context)
    if not flow:
        return False
    if "search" in flow and not await validate_booking_time(
        message, context, SearchConfig.from_mapping(flow["search"])
    ):
        return True
    text = text.strip()
    step = flow["step"]
    if step == "purpose":
        if not text or len(text) > 200:
            await prompt(
                message,
                context,
                flow,
                notice="Send a purpose between 1 and 200 characters.",
            )
            return True
        flow.update(purpose=text, step="co_booker", page=0)
    elif step in {"co_booker", "co_email", "contact_email"}:
        if not SMU_EMAIL_RE.fullmatch(text):
            await prompt(
                message,
                context,
                flow,
                notice="Please send the full SMU email address, such as friend@smu.edu.sg. A username alone isn’t enough.",
            )
            return True
        email = text.casefold()
        if step == "contact_email":
            save_contact(context, flow.pop("name"), email)
            flow.update(step="contacts", page=0)
        else:
            flow["co_booker"] = email
            if any(
                person["email"].casefold() == email
                for person in contacts(context).values()
            ):
                await finish_booking(message, context, flow)
                return True
            flow["step"] = "save_choice"
    elif step in {"save_name", "contact_name"}:
        if not text or len(text) > 40 or any(ord(char) < 32 for char in text):
            await prompt(
                message,
                context,
                flow,
                notice="Send a name between 1 and 40 characters, on one line.",
            )
            return True
        if step == "save_name":
            save_contact(context, text, flow["co_booker"])
            await finish_booking(message, context, flow)
            return True
        flow.update(name=text, step="contact_email")
    else:
        await prompt(
            message,
            context,
            flow,
            notice="Choose an option below, or use /cancel.",
        )
        return True
    flow["expires_at"] = time.time() + 900
    await prompt(message, context, flow)
    return True


async def wizard_callback(update, context):
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Use /book or /cobookers to start again.", show_alert=True)
        return
    flow = current_flow(query.message, context, parts[1])
    if not flow:
        await query.answer(
            "These questions expired. Use /book or /cobookers to start again.",
            show_alert=True,
        )
        return
    action = parts[2]
    value = parts[3] if len(parts) > 3 else ""
    step = flow["step"]
    allowed = {
        "cancel": step != "contacts",
        "done": step == "contacts",
        "use": step == "co_booker",
        "new": step == "co_booker",
        "page": step in {"co_booker", "contacts"},
        "save": step == "save_choice",
        "skip": step == "save_choice",
        "add": step == "contacts",
        "remove": step == "contacts",
        "back": step
        in {
            "co_booker",
            "co_email",
            "save_choice",
            "save_name",
            "contact_name",
            "contact_email",
        },
    }
    if not allowed.get(action):
        await query.answer(
            "That question has already been answered. Use the latest buttons.",
            show_alert=True,
        )
        return
    await query.answer()
    if action not in {"cancel", "done"} and "search" in flow:
        if not await validate_booking_time(
            query.message, context, SearchConfig.from_mapping(flow["search"])
        ):
            return
    flow["expires_at"] = time.time() + 900
    if action in {"cancel", "done"}:
        abandon_setup(context)
        await query.edit_message_text(
            "Booking setup cancelled. No booking was submitted."
            if action == "cancel"
            else "Saved co-bookers are ready for your next booking."
        )
        return
    if action == "use":
        person = contacts(context).get(value)
        if not person:
            await prompt(
                query.message,
                context,
                flow,
                notice="That saved co-booker was removed. Choose another.",
            )
            return
        flow["co_booker"] = person["email"]
        await finish_booking(query.message, context, flow)
        return
    if action == "skip":
        await finish_booking(query.message, context, flow)
        return
    if action == "page":
        try:
            flow["page"] = int(value)
        except ValueError:
            return
    elif action == "new":
        flow["step"] = "co_email"
    elif action == "save":
        flow["step"] = "save_name"
    elif action == "add":
        flow["step"] = "contact_name"
    elif action == "remove":
        contacts(context).pop(value, None)
    elif action == "back":
        flow["step"] = {
            "co_booker": "purpose",
            "co_email": "co_booker",
            "save_choice": "co_booker",
            "save_name": "save_choice",
            "contact_name": "contacts",
            "contact_email": "contact_name",
        }[step]
    await prompt(query.message, context, flow)


async def finish_booking(message, context, flow):
    request = BookingRequest(
        search=SearchConfig.from_mapping(flow["search"]),
        room=flow["room"],
        purpose=flow["purpose"],
        co_booker=flow["co_booker"],
        booking_usage="Meeting",
    )
    await show_review(message, context, request)
    abandon_setup(context)


async def show_review(message, context, request):
    if not await validate_booking_time(message, context, request.search):
        return
    token = secrets.token_urlsafe(8)
    drafts = context.user_data.setdefault("booking_drafts", {})
    # Remove expired drafts so persistence does not grow with each booking.
    for key in list(drafts):
        if drafts[key]["expires_at"] < time.time():
            drafts.pop(key)
    drafts[token] = {"request": request.to_dict(), "expires_at": time.time() + 600}
    if getattr(context, "ui_panel", None):
        context.ui_panel.back_to(f"ui:edit_booking:{token}")
    await render_review(message, request, token)


async def render_review(message, request, token, *, edit=False, notice=""):
    text = (
        "<b>Review booking</b>\n\n"
        f"Room: <code>{escape(request.room)}</code>\n"
        f"Date: {escape(request.search.date)}\n"
        f"Time: {escape(request.search.start_time)}–{escape(request.search.end_time)}\n"
        f"Purpose: {escape(request.purpose)}\nUsage: {escape(request.booking_usage)}\n"
        f"Co-booker: <code>{escape(request.co_booker)}</code>\n\n"
        "Nothing has been submitted. This review expires in 10 minutes.\n"
        "Confirming accepts the FBS acknowledgement and declaration. "
        "Availability will be checked again before submission."
    )
    sender = message.edit_text if edit else message.reply_text
    await sender(
        notice + text,
        parse_mode="HTML",
        reply_markup=Markup(
            [
                [Button("🧪 Validate only", callback_data=f"booking_validate:{token}")],
                [Button("✅ Confirm & book", callback_data=f"booking_confirm:{token}")],
                [Button("❌ Cancel", callback_data=f"booking_cancel:{token}")],
            ]
        ),
    )
