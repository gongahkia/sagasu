"""Private, single-use Telegram reviews for cancelling existing FBS bookings."""

import secrets
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from .auth import owner_auth_notifier
from .fbs_account import (
    fetch_bookings,
    execute_cancellation,
    CancellationOutcomeUnknown,
)
from .fbs_client import FBSError
from .observability import event, observed_operation

REASON = "Cancelled by booker via Sagasu"


@observed_operation("bookings")
async def bookings_command(update, context):
    await show_bookings(update.effective_message, context)


async def show_bookings(message, context):
    if message.chat.type != "private":
        await message.reply_text(
            "Use /bookings in my private chat to manage your bookings."
        )
        return
    status = await message.reply_text("⏳ Reading FBS My Bookings…")
    try:
        result = await fetch_bookings(auth_notify=owner_auth_notifier(context.bot))
    except FBSError as error:
        await status.edit_text(f"⚠️ Could not read My Bookings: {error}")
        return
    listing = {
        "id": secrets.token_urlsafe(6),
        "expires_at": time.time() + 600,
        "chat_id": message.chat_id,
        "items": result["bookings"],
        "total": result["total"],
    }
    context.user_data["booking_management"] = listing
    await render_bookings(status, listing, 0)


async def render_bookings(message, listing, page):
    items = listing["items"]
    page = max(0, min(page, max(0, (len(items) - 1) // 6)))
    text = f"📒 FBS My Bookings · {listing['total']} item(s)\n\n"
    buttons = []
    for index, item in enumerate(items[page * 6 : (page + 1) * 6], start=page * 6):
        text += (
            f"{item['reference_number'][:100]} · {item['room'][:160]}\n"
            f"{item['date_time'][:200]}\nStatus: {item['fbs_status'][:160]}\n"
            + (
                ""
                if item["can_cancel"]
                else "FBS offers no cancellation action for this booking.\n"
            )
            + "\n"
        )
        if item["can_cancel"]:
            buttons.append(
                [
                    Button(
                        f"Cancel {item['room'][:40]} · {item['reference_number'][:40]}",
                        callback_data=f"manage:{listing['id']}:choose:{index}",
                    )
                ]
            )
    if not items:
        text += "No bookings were found."
    if len(items) < listing["total"]:
        text += f"Read {len(items)} of {listing['total']} entries from FBS. Remaining entries are available in FBS My Bookings.\n"
    pages = []
    if page:
        pages.append(
            Button(
                "← Previous", callback_data=f"manage:{listing['id']}:page:{page - 1}"
            )
        )
    if (page + 1) * 6 < len(items):
        pages.append(
            Button("Next →", callback_data=f"manage:{listing['id']}:page:{page + 1}")
        )
    if pages:
        buttons.append(pages)
    text += "\nSelecting Cancel opens a review; it does not cancel immediately."
    await message.edit_text(text, parse_mode=None, reply_markup=Markup(buttons))


@observed_operation("cancellation")
async def management_callback(update, context):
    query = update.callback_query
    await query.answer()
    if query.message.chat.type != "private":
        return
    parts = query.data.split(":")
    if len(parts) < 3:
        return
    _, token, action, *values = parts
    reviews = context.user_data.setdefault("cancellation_reviews", {})
    for key in list(reviews):
        if reviews[key]["expires_at"] < time.time():
            reviews.pop(key)
    if action in {"confirm", "keep"}:
        review = reviews.get(token)
        if not review or review["chat_id"] != query.message.chat_id:
            await query.message.reply_text(
                "This cancellation review expired or was already used. Refresh /bookings."
            )
            return
        reviews.pop(token)
        if action == "keep":
            await query.edit_message_text(
                "Booking kept. No cancellation was submitted."
            )
            return
        status = await query.message.reply_text(
            "⏳ Rechecking the booking before cancellation…"
        )
        try:
            result = await execute_cancellation(
                review["booking"], REASON, auth_notify=owner_auth_notifier(context.bot)
            )
        except CancellationOutcomeUnknown as error:
            context.user_data.pop("booking_management", None)
            await status.edit_text(f"⚠️ {error} This review has been disabled.")
            return
        except FBSError as error:
            await status.edit_text(f"⚠️ {error} Refresh /bookings for a new review.")
            return
        context.user_data.pop("booking_management", None)
        label = (
            "✅ Cancellation verified in FBS My Bookings."
            if result["status"] == "cancelled"
            else "⏳ Cancellation requested; FBS has not marked it cancelled yet."
        )
        await status.edit_text(
            f"{label}\nReference: {review['booking']['reference_number']}\nFBS status: {result['fbs_status']}"
        )
        return
    listing = context.user_data.get("booking_management")
    if (
        not listing
        or listing["id"] != token
        or listing["expires_at"] < time.time()
        or listing["chat_id"] != query.message.chat_id
    ):
        await query.message.reply_text("This booking list expired. Refresh /bookings.")
        return
    try:
        index = int(values[0])
    except (ValueError, IndexError):
        return
    if action == "page":
        await render_bookings(query.message, listing, index)
        return
    if action != "choose" or not 0 <= index < len(listing["items"]):
        return
    item = listing["items"][index]
    if not item["can_cancel"]:
        await query.message.reply_text(
            "FBS offers no cancellation action for this booking."
        )
        return
    key = secrets.token_urlsafe(8)
    reviews.clear()
    reviews[key] = {
        "booking": dict(item),
        "chat_id": query.message.chat_id,
        "expires_at": time.time() + 300,
    }
    event("cancellation.review_created")
    await query.message.reply_text(
        f"Cancel this FBS booking?\n\nReference: {item['reference_number']}\nRoom: {item['room']}\n"
        f"When: {item['date_time']}\nStatus: {item['fbs_status']}\nReason: {REASON}\n\n"
        "Confirming submits cancellation to FBS. This review expires in 5 minutes.",
        parse_mode=None,
        reply_markup=Markup(
            [
                [Button("Confirm cancellation", callback_data=f"manage:{key}:confirm")],
                [Button("Keep booking", callback_data=f"manage:{key}:keep")],
            ]
        ),
    )
