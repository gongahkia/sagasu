import asyncio
import logging
import sys
from pathlib import Path
import os
import time
from datetime import datetime, timedelta
from html import escape
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import MessageHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PicklePersistence,
)

try:
    from .booking_flow import (
        abandon_setup,
        accept_text,
        begin_booking,
        cobookers_command,
        open_contacts,
        show_review,
        wizard_callback,
        validate_booking_time,
        SMU_EMAIL_RE,
    )
    from .auth import owner_auth_notifier
    from .fbs_client import (
        AuthenticationRequired,
        BookingRequest,
        BookingUnavailable,
        BookingOutcomeUnknown,
        FBSError,
        FBS_TIMEZONE,
        LayoutChanged,
        SearchConfig,
        execute_booking,
        check_task_list,
        search_config_from_bot,
        search_fbs,
        require_future_booking,
    )
except ImportError:
    if __package__:
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bot.booking_flow import (
        abandon_setup,
        accept_text,
        begin_booking,
        cobookers_command,
        open_contacts,
        show_review,
        wizard_callback,
        validate_booking_time,
        SMU_EMAIL_RE,
    )
    from bot.auth import owner_auth_notifier
    from bot.fbs_client import (
        AuthenticationRequired,
        BookingRequest,
        BookingUnavailable,
        BookingOutcomeUnknown,
        FBSError,
        FBS_TIMEZONE,
        LayoutChanged,
        SearchConfig,
        execute_booking,
        check_task_list,
        search_config_from_bot,
        search_fbs,
        require_future_booking,
    )


from bot.observability import event, observed_operation, observed_step
from bot.surface import surface_handler, deliver_receipt, finish_operation
from bot.booking_management import bookings_command, management_callback, show_bookings
from bot.task_management import task_callback, listing as task_listing

logger = logging.getLogger(__name__)
_ACTIVE_SEARCHES = {}
_AUTH_HELP = (
    "🔐 Your SMU session uses Sagasu’s private browser profile. "
    "Optional login credentials come from the host’s private configuration. "
    "When Microsoft needs MFA, Sagasu sends an approval prompt to your private Telegram chat."
)


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restrict a deployed personal bot when TELEGRAM_OWNER_ID is configured."""

    owner_id = os.getenv("TELEGRAM_OWNER_ID")
    if not owner_id or (
        update.effective_user and str(update.effective_user.id) == owner_id
    ):
        return
    if update.callback_query:
        await update.callback_query.answer(
            "This is a private Sagasu instance.", show_alert=True
        )
    elif update.effective_message:
        await update.effective_message.reply_text("This is a private Sagasu instance.")
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panel = getattr(context, "ui_panel", None)
    if panel:
        panel.state["screen"] = "home"
        panel.back_to(None)
    abandon_setup(context)
    try:
        window = search_config_from_bot(context.user_data.get("scrape_config") or {})
        window_text = f"{window.date} · {window.start_time}–{window.end_time} SGT"
    except ValueError:
        window_text = "Choose a valid time in Search settings."
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔎 Find available rooms", callback_data="run_script"
                ),
                InlineKeyboardButton("📒 My bookings", callback_data="open_bookings"),
            ],
            [
                InlineKeyboardButton(
                    "👥 Saved co-bookers", callback_data="open_cobookers"
                ),
                InlineKeyboardButton("📋 FBS Task List", callback_data="open_tasks"),
            ],
            [
                InlineKeyboardButton("⚙️ Search settings", callback_data="open_config"),
                InlineKeyboardButton("Help", callback_data="view_help"),
            ],
        ]
    )
    await update.message.reply_text(
        "<b>Sagasu · SMU room bookings</b>\n\n"
        f"Next search: {escape(window_text)}\n\nChoose an action below.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@observed_operation("search")
async def run_script(callback_query: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Searching FBS for available rooms")
    abandon_setup(context)

    try:
        search_config = search_config_from_bot(
            context.user_data.get("scrape_config") or {}
        )
        if not await validate_booking_time(
            callback_query.message, context, search_config
        ):
            return
        cancel_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 Cancel", callback_data="scrape_cancel")]]
        )
        status_msg = await callback_query.message.reply_text(
            "⏳ Searching FBS…", reply_markup=cancel_kb
        )

        async def progress(stage):
            logger.debug("FBS progress: %s", stage)
            try:
                await status_msg.edit_text(f"⏳ {stage}…", reply_markup=cancel_kb)
            except Exception as e:
                logger.debug("Telegram progress update failed (%s)", type(e).__name__)

        scrape_task = asyncio.create_task(
            search_fbs(
                search_config,
                progress,
                auth_notify=owner_auth_notifier(
                    context.bot, render=getattr(context, "ui_auth_notify", None)
                ),
            )
        )
        _ACTIVE_SEARCHES[callback_query.from_user.id] = scrape_task
        try:
            result = await scrape_task
        except asyncio.CancelledError:
            await status_msg.edit_text("🛑 Scrape cancelled.")
            return
        finally:
            _ACTIVE_SEARCHES.pop(callback_query.from_user.id, None)

        if not await validate_booking_time(
            callback_query.message, context, result.config
        ):
            await status_msg.edit_text(
                "Search ended because the requested start time has passed. Choose a future time in /config."
            )
            return
        context.user_data["last_search"] = result.to_dict()
        context.user_data["last_room_order"] = [
            room.name for room in result.bookable_rooms
        ]
        await render_search_results(status_msg, context)

    except AuthenticationRequired as e:
        await callback_query.message.reply_text(f"🔐 {e}")
    except LayoutChanged as e:
        await callback_query.message.reply_text(f"🧩 FBS layout changed: {e}")
    except FBSError as e:
        await callback_query.message.reply_text(f"⚠️ FBS operation failed: {e}")
    except ValueError as error:
        await callback_query.message.reply_text(
            f"⚠️ Invalid search window: {error}. Update /config and search again."
        )
    except Exception as e:
        logger.error("FBS search failed (%s)", type(e).__name__)
        await callback_query.message.reply_text(
            "An error occurred during the scraping process. Report the issue @gongahkia."
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "run_script":
        try:
            await run_script(query, context)
        except Exception as e:
            logger.error("FBS search failed (%s)", type(e).__name__)
            try:
                await query.edit_message_text(
                    "An error occurred during the scraping process. 🌋"
                )
            except Exception as edit_error:
                logger.debug(
                    "Telegram message update failed (%s)", type(edit_error).__name__
                )
    elif query.data == "open_cobookers":
        await open_contacts(query.message, context)
    elif query.data == "open_tasks":
        await show_task_list(query.message, context)
    elif query.data == "open_bookings":
        await show_bookings(query.message, context)
    elif query.data == "open_config":
        abandon_setup(context)
        await query.message.reply_text(
            _search_settings_text(context),
            reply_markup=_build_scrape_menu_keyboard(context),
        )
    elif query.data == "view_help":
        from types import SimpleNamespace

        await help_command(SimpleNamespace(message=query.message), context)
    elif query.data == "settings":
        await query.message.reply_text(_AUTH_HELP)


from .personal_options import SCRAPE_PARAM_CHOICES



def get_scrape_config(context):
    return context.user_data.setdefault("scrape_config", {})


def _build_scrape_menu_keyboard(context):
    cfg = get_scrape_config(context)

    def label(key, fallback):
        return cfg.get(key, fallback)

    try:
        effective = search_config_from_bot(cfg)
    except ValueError:
        effective = None
    date_raw = cfg.get("date_raw") or (effective.date if effective else "Today")
    if not cfg.get("date_raw") and not cfg.get("start_time"):
        date_raw += " (rolling)"
    start = cfg.get("start_time") or (
        effective.start_time if effective else "Next half-hour"
    )
    start += " SGT" + (" (rolling)" if not cfg.get("start_time") else "")
    kb = [
        [InlineKeyboardButton(f"📅 Date: {date_raw}", callback_data="pick:date")],
        [
            InlineKeyboardButton(
                f"⏰ Start: {start}",
                callback_data="pick:start_time",
            )
        ],
        [
            InlineKeyboardButton(
                f"⏳ Duration: {label('duration_hrs', 2):g}h",
                callback_data="pick:duration",
            )
        ],
        [
            InlineKeyboardButton(
                f"👥 Capacity: {label('room_capacity', 'Any')}",
                callback_data="pick:capacity",
            )
        ],
        [
            InlineKeyboardButton(
                f"🏢 Buildings: {len(cfg.get('buildings') or [])} selected",
                callback_data="pick:building",
            )
        ],
        [
            InlineKeyboardButton(
                f"🪜 Floors: {len(cfg.get('floors') or [])} selected",
                callback_data="pick:floor",
            )
        ],
        [
            InlineKeyboardButton(
                f"🛋️ Facility: {len(cfg.get('facility_types') or [])} selected",
                callback_data="pick:facility_type",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔌 Equipment: {len(cfg.get('equipment') or [])} selected",
                callback_data="pick:equipment",
            )
        ],
        [InlineKeyboardButton("↩️ Reset to defaults", callback_data="pick:reset")],
        [InlineKeyboardButton("← Home", callback_data="ui:home")],
    ]
    return InlineKeyboardMarkup(kb)


def _search_settings_text(context):
    text = "⚙️ Search settings — tap a row to change:"
    try:
        config = get_scrape_config(context)
        resolved = search_config_from_bot(config)
        text += (
            f"\nWindow: {resolved.date}, {resolved.start_time}–{resolved.end_time} SGT"
        )
        if not config.get("start_time"):
            text += "\nThe rolling start recalculates for each search."
        require_future_booking(resolved)
    except (BookingUnavailable, ValueError) as error:
        text += f"\n\n⚠️ {error}"
    return text


async def scrape_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    abandon_setup(context)
    await update.message.reply_text(
        _search_settings_text(context),
        reply_markup=_build_scrape_menu_keyboard(context),
    )


async def _edit_settings_message(query, text, **kwargs):
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as error:
        if "message is not modified" not in str(error).casefold():
            raise
        # Done, Reset and repeated selections can legitimately render the same
        # text and buttons. Telegram rejects these edits, although state is valid.
        logger.debug("Telegram settings already match the selected configuration")
        return None


async def _open_param_picker(query, context, param):
    cfg = get_scrape_config(context)
    if param == "date":
        buttons = [
            [InlineKeyboardButton(lbl, callback_data=f"set:date:{off}")]
            for lbl, off in SCRAPE_PARAM_CHOICES["date"]
        ]
        buttons.insert(
            0, [InlineKeyboardButton("↻ Rolling date", callback_data="set:date:AUTO")]
        )
    elif param == "start_time":
        choices = SCRAPE_PARAM_CHOICES["start_time"]
        buttons = [
            [
                InlineKeyboardButton(t, callback_data=f"set:start_time:{t}")
                for t in choices[index : index + 3]
            ]
            for index in range(0, len(choices), 3)
        ]
        buttons.insert(
            0,
            [
                InlineKeyboardButton(
                    "↻ Next half-hour (rolling)", callback_data="set:start_time:AUTO"
                )
            ],
        )
    elif param == "duration":
        buttons = [
            [InlineKeyboardButton(f"{d}h", callback_data=f"set:duration:{d}")]
            for d in SCRAPE_PARAM_CHOICES["duration"]
        ]
    elif param == "capacity":
        buttons = [
            [InlineKeyboardButton(lbl, callback_data=f"set:capacity:{val or 'ANY'}")]
            for lbl, val in SCRAPE_PARAM_CHOICES["capacity"]
        ]
    elif param in ("building", "floor", "facility_type", "equipment"):
        key_map = {
            "building": "buildings",
            "floor": "floors",
            "facility_type": "facility_types",
            "equipment": "equipment",
        }
        selected = set(cfg.get(key_map[param]) or [])
        buttons = []
        for i, opt in enumerate(SCRAPE_PARAM_CHOICES[param]):
            mark = "✅ " if opt in selected else "◻️ "
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{mark}{opt}", callback_data=f"toggle:{param}:{i}"
                    )
                ]
            )
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="pick:done")])
    else:
        return
    if param in ("date", "start_time", "duration", "capacity"):
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="pick:done")])
    await _edit_settings_message(
        query,
        f"Choose {param.replace('_', ' ')}:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def scrape_config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as error:
        if "query is too old" not in str(error).casefold():
            raise
        await query.message.reply_text(
            "That button request expired. Choose again from the refreshed settings.\n\n"
            + _search_settings_text(context),
            reply_markup=_build_scrape_menu_keyboard(context),
        )
        return
    data = query.data
    cfg = get_scrape_config(context)
    if data.startswith("pick:"):
        param = data.split(":", 1)[1]
        if param == "done":
            await _edit_settings_message(
                query,
                _search_settings_text(context),
                reply_markup=_build_scrape_menu_keyboard(context),
            )
            return
        if param == "reset":
            context.user_data["scrape_config"] = {}
            await _edit_settings_message(
                query,
                _search_settings_text(context),
                reply_markup=_build_scrape_menu_keyboard(context),
            )
            return
        await _open_param_picker(query, context, param)
    elif data.startswith("set:"):
        _, param, value = data.split(":", 2)
        if param == "date":
            if value == "AUTO":
                cfg.pop("date_raw", None)
            else:
                offset_days = int(value)
                target = datetime.now(FBS_TIMEZONE) + timedelta(days=offset_days)
                cfg["date_raw"] = target.strftime("%-d %B %Y").lower()
        elif param == "start_time":
            if value == "AUTO":
                cfg.pop("start_time", None)
            else:
                cfg["start_time"] = value
            cfg.pop("end_time", None)
        elif param == "duration":
            cfg["duration_hrs"] = float(value)
            cfg.pop("end_time", None)
        elif param == "capacity":
            cfg["room_capacity"] = None if value == "ANY" else value
        await _edit_settings_message(
            query,
            _search_settings_text(context),
            reply_markup=_build_scrape_menu_keyboard(context),
        )
    elif data.startswith("toggle:"):
        _, param, idx_s = data.split(":", 2)
        idx = int(idx_s)
        key_map = {
            "building": "buildings",
            "floor": "floors",
            "facility_type": "facility_types",
            "equipment": "equipment",
        }
        key = key_map[param]
        current = list(cfg.get(key) or [])
        opt = SCRAPE_PARAM_CHOICES[param][idx]
        if opt in current:
            current.remove(opt)
        else:
            current.append(opt)
        cfg[key] = current
        await _open_param_picker(query, context, param)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await accept_text(update.message, context, update.message.text or ""):
        return
    await update.message.reply_text(
        "Use /start to find a room, /book for guided booking, or /cobookers to manage saved co-bookers."
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_AUTH_HELP)


async def room_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":", 1)[1])
    if getattr(context, "ui_panel", None):
        context.ui_panel.back_to(f"ui:results:{max(0, idx) // 8}")
    order = context.user_data.get("last_room_order") or []
    if idx >= len(order):
        await query.message.reply_text("Results expired — run a new scrape.")
        return
    room = order[idx]
    search = context.user_data.get("last_search") or {}
    if not search.get("config"):
        await query.message.reply_text("Results expired — run a new search.")
        return
    if not await validate_booking_time(
        query.message, context, SearchConfig.from_mapping(search["config"])
    ):
        return
    room_data = next(
        (
            candidate
            for candidate in search.get("rooms", [])
            if candidate["name"] == room
        ),
        None,
    )
    if not room_data:
        await query.message.reply_text("Results expired — run a new scrape.")
        return
    text = f"<code>{escape(room)}</code> 🏠\n\n"
    for slot in room_data.get("timeslots", []):
        icon = "✅" if slot["status"] == "free" else "❌"
        text += (
            f"<i>{escape(slot['start'])}–{escape(slot['end'])}</i> — "
            f"{escape(slot['status'].title())} {icon}\n"
        )
    context.user_data["selected_room"] = room
    await query.message.reply_text(
        text + "\nTap below to answer the booking questions.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Book this room →", callback_data=f"select_room:{idx}"
                    )
                ]
            ]
        ),
    )


async def select_room_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":", 1)[1])
    order = context.user_data.get("last_room_order") or []
    if idx >= len(order):
        await query.message.reply_text("Results expired — run a new search.")
        return
    context.user_data["selected_room"] = order[idx]
    await begin_booking(query.message, context)


async def autopick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get("last_room_order") or []
    if not order:
        await query.message.reply_text("Results expired — run a new search.")
        return
    context.user_data["selected_room"] = order[0]
    await begin_booking(query.message, context)


async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args or []).strip()
    if "|" not in raw:
        await begin_booking(update.message, context, purpose=raw or None)
        return
    room = context.user_data.get("selected_room")
    search = context.user_data.get("last_search") or {}
    if not room or not search.get("config"):
        await update.message.reply_text("Run a search and select a room first.")
        return
    parts = [part.strip() for part in raw.split("|")]
    if not parts[0] or len(parts) < 2 or not SMU_EMAIL_RE.fullmatch(parts[1]):
        await update.message.reply_text("Let’s do this one question at a time.")
        await begin_booking(update.message, context, purpose=parts[0] or None)
        return
    abandon_setup(context)
    context.user_data.pop("booking_drafts", None)
    await show_review(
        update.message,
        context,
        BookingRequest(
            search=SearchConfig.from_mapping(search["config"]),
            room=room,
            purpose=parts[0],
            co_booker=parts[1].casefold(),
            booking_usage=parts[2] if len(parts) > 2 and parts[2] else "Meeting",
        ),
    )


@observed_operation("booking")
async def booking_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, token = query.data.split(":", 1)
    drafts = context.user_data.get("booking_drafts") or {}
    draft = drafts.get(token)
    if not draft or draft["expires_at"] < time.time():
        drafts.pop(token, None)
        await query.edit_message_text(
            "This booking confirmation expired. Create a new one."
        )
        return
    if action == "booking_cancel":
        drafts.pop(token, None)
        await query.edit_message_text(
            "Booking cancelled locally; nothing was submitted."
        )
        return

    submit = action == "booking_confirm"
    request = BookingRequest.from_mapping(draft["request"])
    if not await validate_booking_time(query.message, context, request.search):
        return
    if submit:
        drafts.pop(token, None)
    elif getattr(context, "ui_panel", None):
        context.ui_panel.back_to(f"ui:edit_booking:{token}")
    status = await query.message.reply_text(
        "⏳ Rechecking availability and preparing the booking…"
    )

    async def progress(stage):
        logger.debug("FBS progress: %s", stage)
        try:
            await status.edit_text(f"⏳ {stage}…")
        except Exception:
            pass

    try:
        try:
            result = await execute_booking(
                request,
                submit=submit,
                progress=progress,
                auth_notify=owner_auth_notifier(
                    context.bot, render=getattr(context, "ui_auth_notify", None)
                ),
            )
        finally:
            finish_operation(context)
        if submit:
            drafts.pop(token, None)
            await status.edit_text(
                "✅ Booking recorded and verified in FBS My Bookings.\n"
                f"Room: {request.room}\n"
                f"Date: {request.search.date}\n"
                f"Time: {request.search.start_time}–{request.search.end_time}\n"
                f"Reference: {result.get('reference_number', 'not shown')}\n"
                f"FBS status: {result.get('fbs_status', 'not shown')}"
            )
            if result.get("receipt_png"):
                await send_booking_receipt(query, context, result)
            elif result.get("receipt_error"):
                await query.message.reply_text(
                    result["receipt_error"]
                    + f"\nReference: {result.get('reference_number', 'not shown')}"
                )
        else:
            from .booking_flow import render_review

            await render_review(
                status,
                request,
                token,
                edit=True,
                notice="🧪 Validation passed. No booking was submitted.\n\n",
            )
    except BookingOutcomeUnknown as error:
        drafts.pop(token, None)
        await status.edit_text(
            f"⚠️ {error} This review has been disabled to prevent a repeated submission."
        )
    except BookingUnavailable as error:
        drafts.pop(token, None)
        await status.edit_text(f"⚠️ Booking cannot proceed: {error}")
    except FBSError as error:
        await status.edit_text(f"⚠️ Booking failed safely before completion: {error}")


@observed_step("receipt.deliver")
async def send_booking_receipt(query, context, result):
    """Deliver private evidence without changing a verified booking's outcome."""
    caption = (
        f"FBS booking details · {result['reference_number']}\n"
        f"{result.get('room', '')}\n"
        f"{result.get('date', '')} · {result.get('start_time', '')}–{result.get('end_time', '')} SGT\n"
        f"FBS status: {result.get('fbs_status', 'not shown')}"
    )
    if getattr(context, "ui_panel", None):
        try:
            await deliver_receipt(
                context, query.from_user.id, result["receipt_png"], caption
            )
            event("receipt.delivered", transport="single_receipt_slot")
        except Exception as error:
            event(
                "receipt.delivery_failed",
                level=logging.WARNING,
                error_type=type(error).__name__,
            )
            await query.message.reply_text(
                f"Your booking is recorded, but the receipt could not be delivered.\nReference: {result.get('reference_number', 'not shown')}\nCheck this reference in FBS My Bookings."
            )
        return

    try:
        event(
            "receipt.delivery_started",
            transport="photo",
            bytes=len(result["receipt_png"]),
        )
        await context.bot.send_photo(
            chat_id=query.from_user.id,
            photo=result["receipt_png"],
            caption=caption[:1024],
            parse_mode=None,
        )
        event("receipt.delivered", transport="photo")
        return
    except Exception as error:
        event(
            "receipt.delivery_retry",
            level=logging.WARNING,
            transport="document",
            error_type=type(error).__name__,
        )
        logger.info(
            "Receipt photo delivery failed (%s); trying PNG file", type(error).__name__
        )
    try:
        await context.bot.send_document(
            chat_id=query.from_user.id,
            document=result["receipt_png"],
            filename="sagasu-booking-details.png",
            caption=caption[:1024],
            parse_mode=None,
        )
        event("receipt.delivered", transport="document")
    except Exception as error:
        event(
            "receipt.delivery_failed",
            level=logging.WARNING,
            error_type=type(error).__name__,
        )
        logger.warning("Receipt delivery unavailable (%s)", type(error).__name__)
        await query.message.reply_text(
            "Your booking is recorded, but I couldn't deliver its screenshot. "
            "Open my private chat and send /start to allow private receipts. "
            "You can view this reference in FBS My Bookings; do not book again."
        )


@observed_operation("tasks")
async def show_task_list(message, context):
    if message.chat.type != "private":
        await message.reply_text(
            "Use /tasks in my private chat to view your FBS Task List."
        )
        return
    status = await message.reply_text("⏳ Checking your FBS Task List…")

    async def progress(stage):
        try:
            await status.edit_text(f"⏳ {stage}…")
        except Exception:
            pass

    try:
        result = await check_task_list(
            progress,
            auth_notify=owner_auth_notifier(
                context.bot, render=getattr(context, "ui_auth_notify", None)
            ),
        )
    except FBSError as error:
        await status.edit_text(f"⚠️ Could not check Task List: {error}")
        return
    context.user_data["task_list_result"] = result
    context.user_data.pop("task_list_view", None)
    context.user_data.pop("task_approval_reviews", None)
    await render_tasks(status, context, 0)


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_task_list(update.message, context)


async def scrape_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task = _ACTIVE_SEARCHES.get(query.from_user.id)
    if task and not task.done():
        task.cancel()
        try:
            await query.edit_message_text("🛑 Cancelling scrape…")
        except Exception:
            pass
    else:
        await query.message.edit_text(
            "Search stopped. Change the search settings or return Home."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = context.user_data.pop("booking_wizard", None)
    drafts = context.user_data.pop("booking_drafts", None)
    cancellations = context.user_data.pop("cancellation_reviews", None)
    approvals = context.user_data.pop("task_approval_reviews", None)
    state = context.user_data.pop("settings_state", None)
    await update.message.reply_text(
        "Cancelled the current questions and pending action reviews. No action was submitted."
        if active or drafts or state or cancellations or approvals
        else "There’s nothing waiting to be cancelled."
    )


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "selected_room",
        "last_search",
        "last_room_order",
        "booking_drafts",
        "booking_wizard",
        "cancellation_reviews",
        "booking_management",
        "email",
        "password",
        "settings_state",
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "Cleared Telegram search and booking state. To end the SMU browser "
        "session too, sign out in the Chrome window."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.user_data.get("scrape_config") or {}

    lines = ["<b>🧾 Current status</b>"]
    lines.append("<i>Authentication:</i> private local Chrome session")
    lines.append(
        f"<i>Selected room:</i> {escape(context.user_data.get('selected_room') or 'none')}"
    )
    lines.append("")
    lines.append("<b>Scrape params</b> (unset = default)")
    try:
        resolved = search_config_from_bot(cfg)
        lines.append(
            f"<i>Window:</i> {resolved.date}, {resolved.start_time}–{resolved.end_time} SGT"
        )
        if not cfg.get("start_time"):
            lines.append("<i>Start:</i> next half-hour (rolling)")
    except ValueError:
        lines.append("<i>Window:</i> invalid — update /config")
    lines.append(f"<i>Duration:</i> {cfg.get('duration_hrs', 2):g}h")
    lines.append(f"<i>Capacity:</i> {cfg.get('room_capacity') or 'any (default)'}")
    lines.append(
        f"<i>Buildings:</i> {', '.join(cfg.get('buildings') or []) or '(default)'}"
    )
    lines.append(f"<i>Floors:</i> {', '.join(cfg.get('floors') or []) or '(default)'}")
    lines.append(
        f"<i>Facility types:</i> {', '.join(cfg.get('facility_types') or []) or '(default)'}"
    )
    lines.append(
        f"<i>Equipment:</i> {', '.join(cfg.get('equipment') or []) or 'any (default)'}"
    )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<code>Sagasu</code> finds and books SMU rooms.\n\n"
        "1. /config — choose date, time and filters\n"
        "2. /start → Find available rooms — search FBS\n"
        "3. Select a room or auto-pick\n"
        "4. Answer the purpose and co-booker questions\n"
        "5. Validate, then explicitly confirm the booking\n\n"
        "/book — start booking questions for the selected room\n"
        "/cobookers — save names/emails or remove saved co-bookers\n"
        "/tasks — check your FBS Task List for requests awaiting action\n"
        "/bookings — view existing bookings and review cancellation\n"
        "/cancel — stop questions and discard pending booking reviews\n\n"
        "Your SMU session stays in a private browser profile; approve MFA on your phone when prompted.",
        parse_mode=ParseMode.HTML,
    )


PERSISTENCE_PATH = os.path.join(os.path.dirname(__file__), "bot_state.pickle")


async def render_search_results(message, context, page=0):
    cached = context.user_data.get("last_search") or {}
    if not cached.get("config"):
        await message.edit_text(
            "No current search results. Choose Find rooms from Home."
        )
        return
    config = SearchConfig.from_mapping(cached["config"])
    if not await validate_booking_time(message, context, config):
        return
    names = context.user_data.get("last_room_order") or []
    page = max(0, min(page, max(0, (len(names) - 1) // 8)))
    text = f"<b>Available rooms · {len(names)}</b>\n{escape(config.date)} · {config.start_time}–{config.end_time} SGT\n\n"
    buttons = []
    if names:
        text += "Choose a room to see its details."
        buttons.append(
            [InlineKeyboardButton("✨ Auto-pick a room", callback_data="autopick")]
        )
        for index, name in enumerate(names[page * 8 : (page + 1) * 8], start=page * 8):
            buttons.append([InlineKeyboardButton(name, callback_data=f"room:{index}")])
        pagination = []
        if page:
            pagination.append(
                InlineKeyboardButton(
                    "← Previous", callback_data=f"ui:results:{page - 1}"
                )
            )
        if (page + 1) * 8 < len(names):
            pagination.append(
                InlineKeyboardButton("Next →", callback_data=f"ui:results:{page + 1}")
            )
        if pagination:
            buttons.append(pagination)
        text += f"\nPage {page + 1} of {max(1, (len(names) + 7) // 8)}"
    else:
        text += "No room is free for this whole window. Change the date, time or filters and search again."
    buttons.append(
        [InlineKeyboardButton("⚙️ Change search", callback_data="open_config")]
    )
    await message.edit_text(
        text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons)
    )


async def render_tasks(message, context, page=0):
    result = context.user_data.get("task_list_result") or {
        "tasks": [],
        "total": 0,
        "complete": True,
    }
    tasks = result["tasks"]
    page = max(0, min(page, max(0, (len(tasks) - 1) // 4)))
    text = f"📋 FBS Task List · {result['total']} item(s)\n\n"
    view = task_listing(context, message.chat_id)
    buttons = []
    if not result["total"]:
        text += "Your FBS Task List is empty. There are no items awaiting your action."
    for index, task in enumerate(tasks[page * 4 : (page + 1) * 4], start=page * 4):
        text += (
            f"{task['reference_number'][:100]} · {task['room'][:150]}\n"
            f"{task['date_time'][:200]}\nRequest: {task['request_type'][:150]}\n"
            f"Requested by: {task['requested_by'][:150]}\nStatus: {task['status'][:150]}\n\n"
        )
        if view and task.get("can_view"):
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"📷 Details · {task['reference_number'][:40]}",
                        callback_data=f"task:{view['id']}:view:{index}",
                    )
                ]
            )
        if view and task.get("can_approve"):
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Review approval · {task['reference_number'][:40]}",
                        callback_data=f"task:{view['id']}:approve:{index}",
                    )
                ]
            )
    if tasks:
        text += "Approval opens a review first. Actions unavailable here can be reviewed in FBS.\n"
    if not result["complete"]:
        text += f"Showing {len(tasks)} of {result['total']} tasks; open FBS for the remaining items.\n"
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton("← Previous", callback_data=f"ui:tasks:{page - 1}")
        )
    if (page + 1) * 4 < len(tasks):
        nav.append(InlineKeyboardButton("Next →", callback_data=f"ui:tasks:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton("↻ Refresh Task List", callback_data="open_tasks")]
    )
    await message.edit_text(
        text, parse_mode=None, reply_markup=InlineKeyboardMarkup(buttons)
    )


async def surface_callback(update, context):
    from types import SimpleNamespace

    query = update.callback_query
    data = query.data
    if data.startswith("ui:"):
        await query.answer()
        parts = data.split(":")
        action = parts[1]
        if action == "home":
            abandon_setup(context)
            context.user_data.pop("booking_drafts", None)
            context.user_data.pop("cancellation_reviews", None)
            context.user_data.pop("task_approval_reviews", None)
            await start(SimpleNamespace(message=query.message), context)
        elif action == "results":
            abandon_setup(context)
            context.user_data.pop("booking_drafts", None)
            await render_search_results(
                query.message, context, int(parts[2]) if len(parts) > 2 else 0
            )
        elif action == "tasks":
            await render_tasks(query.message, context, int(parts[2]))
        elif action == "edit_booking" and len(parts) == 3:
            from .booking_flow import new_flow, prompt

            draft = (context.user_data.get("booking_drafts") or {}).pop(parts[2], None)
            if not draft or draft["expires_at"] < time.time():
                await query.message.edit_text(
                    "That booking review expired. Search again from Home."
                )
                return
            request = BookingRequest.from_mapping(draft["request"])
            if not await validate_booking_time(query.message, context, request.search):
                return
            flow = new_flow(
                query.message,
                context,
                "purpose",
                room=request.room,
                search=draft["request"]["search"],
                purpose=request.purpose,
                co_booker=request.co_booker,
            )
            context.ui_panel.back_to("ui:results:0")
            await prompt(query.message, context, flow)
        return
    routes = [
        ("task:", task_callback),
        ("manage:", management_callback),
        ("wizard:", wizard_callback),
        ("pick:", scrape_config_callback),
        ("set:", scrape_config_callback),
        ("toggle:", scrape_config_callback),
        ("room:", room_details_callback),
        ("select_room:", select_room_callback),
        ("booking_", booking_action_callback),
        ("scrape_cancel", scrape_cancel_callback),
        ("autopick", autopick_callback),
    ]
    for prefix, handler in routes:
        if data.startswith(prefix):
            await handler(update, context)
            return
    await button_callback(update, context)


async def telegram_error(update, context):
    from telegram.error import Conflict, InvalidToken

    if isinstance(context.error, (Conflict, InvalidToken)):
        logger.error(
            "Telegram rejected polling. Check BOT_TOKEN and stop any other instance of this bot."
        )
        context.application.stop_running()
        return
    if isinstance(context.error, BadRequest):
        detail = str(context.error).casefold()
        if "message is not modified" in detail:
            logger.debug("Telegram message already matches the requested content")
            return
        reasons = {
            "query is too old": "button request expired; reopen the menu",
            "message to edit not found": "message to edit no longer exists; reopen the menu",
            "message can't be edited": "message cannot be edited; reopen the menu",
            "can't parse entities": "message formatting was rejected",
            "button_data_invalid": "button callback data was rejected",
        }
        reason = next(
            (hint for marker, hint in reasons.items() if marker in detail),
            "unrecognized request rejection; raw details suppressed",
        )
        logger.error("Telegram update failed (BadRequest): %s", reason)
        return
    logger.error(
        "Telegram update failed (%s); raw details suppressed",
        type(context.error).__name__,
    )


def build_application(token, persistence_path=PERSISTENCE_PATH, *, post_init=None):
    persistence = PicklePersistence(filepath=str(persistence_path))
    app = (
        ApplicationBuilder()
        .token(token)
        .persistence(persistence)
        .concurrent_updates(8)
        .post_init(post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.ALL, access_guard), group=-1)
    app.add_handler(CallbackQueryHandler(access_guard), group=-1)
    app.add_handler(
        CallbackQueryHandler(surface_handler(surface_callback), pattern=r"^u:")
    )
    app.add_handler(CommandHandler("start", surface_handler(start)))
    app.add_handler(CommandHandler("help", surface_handler(help_command)))
    app.add_handler(CommandHandler("settings", surface_handler(settings_command)))
    app.add_handler(CommandHandler("logout", surface_handler(logout_command)))
    app.add_handler(CommandHandler("config", surface_handler(scrape_config_command)))
    app.add_handler(CommandHandler("cancel", surface_handler(cancel_command)))
    app.add_handler(CommandHandler("status", surface_handler(status_command)))
    app.add_handler(CommandHandler("book", surface_handler(book_command)))
    app.add_handler(CommandHandler("cobookers", surface_handler(cobookers_command)))
    app.add_handler(
        CommandHandler(["tasks", "tasklist"], surface_handler(tasks_command))
    )
    app.add_handler(
        CommandHandler(["bookings", "cancelbooking"], surface_handler(bookings_command))
    )
    app.add_handler(
        CallbackQueryHandler(surface_handler(management_callback), pattern=r"^manage:")
    )
    app.add_handler(
        CallbackQueryHandler(surface_handler(task_callback), pattern=r"^task:")
    )
    app.add_handler(
        CallbackQueryHandler(surface_handler(wizard_callback), pattern=r"^wizard:")
    )
    app.add_handler(
        CallbackQueryHandler(
            surface_handler(scrape_config_callback), pattern=r"^(pick|set|toggle):"
        )
    )
    app.add_handler(
        CallbackQueryHandler(surface_handler(room_details_callback), pattern=r"^room:")
    )
    app.add_handler(
        CallbackQueryHandler(
            surface_handler(select_room_callback), pattern=r"^select_room:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(surface_handler(autopick_callback), pattern=r"^autopick$")
    )
    app.add_handler(
        CallbackQueryHandler(
            surface_handler(booking_action_callback),
            pattern=r"^booking_(validate|confirm|cancel):",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            surface_handler(scrape_cancel_callback), pattern=r"^scrape_cancel$"
        )
    )
    app.add_handler(CallbackQueryHandler(surface_handler(button_callback)))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, surface_handler(handle_text_input)
        )
    )
    app.add_error_handler(telegram_error)
    return app


def main():
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bot.cli import main as cli_main

    return cli_main(sys.argv[1:] or ["run"])


if __name__ == "__main__":
    raise SystemExit(main())
