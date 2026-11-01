import asyncio
import logging
import sys
from pathlib import Path
import os
import time
from datetime import datetime, timedelta
from html import escape
from telegram.constants import ParseMode
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
    abandon_setup(context)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔎 Find available rooms", callback_data="run_script"
                )
            ],
            [InlineKeyboardButton("⚙️ Search settings", callback_data="open_config")],
            [
                InlineKeyboardButton(
                    "👥 Saved co-bookers", callback_data="open_cobookers"
                )
            ],
            [InlineKeyboardButton("Help", callback_data="view_help")],
            [InlineKeyboardButton("📋 FBS Task List", callback_data="open_tasks")],
        ]
    )
    await update.message.reply_text(
        "<b>Sagasu · SMU room bookings</b>\n\n"
        "Find a room, then I’ll guide you through the booking questions.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


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
            "⏳ Starting scrape…", reply_markup=cancel_kb
        )

        async def progress(stage):
            logger.info("FBS: %s", stage)
            try:
                await status_msg.edit_text(f"⏳ {stage}…", reply_markup=cancel_kb)
            except Exception as e:
                logger.debug("Telegram progress update failed (%s)", type(e).__name__)

        scrape_task = asyncio.create_task(
            search_fbs(
                search_config, progress, auth_notify=owner_auth_notifier(context.bot)
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
        rooms = result.bookable_rooms
        context.user_data["last_room_order"] = [room.name for room in rooms]
        if not rooms:
            await status_msg.edit_text(
                "😴 No room is free for the entire requested window.\n"
                "Adjust /config and try again."
            )
        else:
            summary = (
                f"<b>🥳 {len(rooms)} room(s) free for the full window</b>\n"
                f"{escape(result.config.date)}, "
                f"{escape(result.config.start_time)}–{escape(result.config.end_time)}\n\n"
            )
            buttons = [
                [
                    InlineKeyboardButton(
                        "✨ Auto-pick best room", callback_data="autopick"
                    )
                ]
            ]
            for idx, room in enumerate(rooms[:20]):
                summary += f"• <code>{escape(room.name)}</code>\n"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"🏠 {room.name}", callback_data=f"room:{idx}"
                        )
                    ]
                )
            await status_msg.edit_text(
                summary,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )

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
        new_keyboard = [
            [
                InlineKeyboardButton(
                    "Oke the script is running 🏃...", callback_data="disabled"
                )
            ]
        ]
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(new_keyboard)
        )
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
    elif query.data == "open_config":
        abandon_setup(context)
        await query.message.reply_text(
            _search_settings_text(context),
            reply_markup=_build_scrape_menu_keyboard(context),
        )
    elif query.data == "view_help":
        await query.edit_message_text(
            "<code>Sagasu</code> finds and books SMU rooms.\n\n"
            "Type /config to set search filters\n"
            "Type /start to search\n"
            "Select or auto-pick a room\n"
            "Answer the booking questions one at a time\n"
            "Use /cobookers to manage saved co-bookers\n"
            "Use /tasks to check pending FBS tasks\n"
            "Type /status to view current settings",
            parse_mode=ParseMode.HTML,
        )
    elif query.data == "settings":
        await query.message.reply_text(_AUTH_HELP)


SCRAPE_PARAM_CHOICES = {
    "date": [
        ("Today", 0),
        ("Tomorrow", 1),
        ("+2 days", 2),
        ("+3 days", 3),
        ("+4 days", 4),
        ("+5 days", 5),
        ("+6 days", 6),
    ],
    "start_time": [
        f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)
    ],
    "duration": [0.5, 1, 1.5, 2, 2.5, 3, 4],
    "capacity": [
        ("Any", None),
        ("<5 pax", "LessThan5Pax"),
        ("6-10 pax", "From6To10Pax"),
        ("11-15 pax", "From11To15Pax"),
        ("16-20 pax", "From16To20Pax"),
        ("21-50 pax", "From21To50Pax"),
        (">50 pax", "From51To100Pax"),
    ],
    "building": [
        "School of Economics/School of Computing & Information Systems 2",
        "School of Computing & Information Systems 1",
        "School of Accountancy",
        "Yong Pung How School of Law/Kwa Geok Choo Law Library",
        "School of Social Sciences/College of Integrative Studies",
        "Lee Kong Chian School of Business",
        "Li Ka Shing Library",
        "Lazada One",
        "NTUC Trade Union House",
        "Administration Building",
        "Sports & Recreation Centre",
        "Campus Centre",
        "Prinsep Street Residences",
        "Concourse - Room/Lab",
        "Campus Open Spaces - Events/Activities",
        "SMU Connexion",
    ],
    "floor": [
        "Basement 1",
        "Basement 2",
        "Level 1",
        "Level 2",
        "Level 3",
        "Level 4",
        "Level 5",
        "Level 6",
        "Level 7",
    ],
    "facility_type": [
        "Group Study Room",
        "Project Room",
        "Meeting Room",
        "Seminar Room",
        "Classroom",
        "Chatterbox",
        "Hostel Facilities",
        "Meeting Pod",
        "MPH / Sports Hall",
        "Phone Booth",
        "Project Room (Level 5)",
        "SMUC Facilities",
        "Student Activities Area",
        "Study Booth",
    ],
    "equipment": [
        "Classroom PC",
        "Classroom Prompter",
        "Clip-on Mic",
        "Doc Camera",
        "DVD Player",
        "Gooseneck Mic",
        "Handheld Mic",
        "Hybrid (USB connection)",
        "In-room VC System",
        "Projector",
        "Rostrum Mic",
        "Teams Room",
        "Teams Room NEAT Board",
        "TV Panel",
        "USB Connection VC room",
        "Video Recording",
        "Wired Mic",
        "Wireless Projection",
    ],
}


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
        [InlineKeyboardButton("✅ Done", callback_data="pick:done")],
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
    await query.edit_message_text(
        f"Select {param}:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def scrape_config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    cfg = get_scrape_config(context)
    if data.startswith("pick:"):
        param = data.split(":", 1)[1]
        if param == "done":
            await query.edit_message_text(
                _search_settings_text(context),
                reply_markup=_build_scrape_menu_keyboard(context),
            )
            return
        if param == "reset":
            context.user_data["scrape_config"] = {}
            await query.edit_message_text(
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
        await query.edit_message_text(
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
    status = await query.message.reply_text(
        "⏳ Rechecking availability and preparing the booking…"
    )

    async def progress(stage):
        logger.info("FBS: %s", stage)
        try:
            await status.edit_text(f"⏳ {stage}…")
        except Exception:
            pass

    try:
        result = await execute_booking(
            request,
            submit=submit,
            progress=progress,
            auth_notify=owner_auth_notifier(context.bot),
        )
        if submit:
            drafts.pop(token, None)
            await status.edit_text(
                "✅ Booking confirmed and verified in FBS My Bookings.\n"
                f"Room: {request.room}\n"
                f"Date: {request.search.date}\n"
                f"Time: {request.search.start_time}–{request.search.end_time}\n"
                f"Reference: {result.get('reference_number', 'not shown')}\n"
                f"FBS status: {result.get('fbs_status', 'not shown')}"
            )
            if result.get("receipt_png"):
                await send_booking_receipt(query, context, result)
            elif result.get("receipt_error"):
                await query.message.reply_text(result["receipt_error"])
        else:
            await status.edit_text(
                "🧪 Validation passed through the complete pre-confirmation flow. "
                "No booking was submitted."
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


async def send_booking_receipt(query, context, result):
    """Deliver private evidence without changing a verified booking's outcome."""
    caption = (
        f"FBS booking details · {result['reference_number']}\n"
        f"{result.get('room', '')}\n"
        f"{result.get('date', '')} · {result.get('start_time', '')}–{result.get('end_time', '')} SGT\n"
        f"FBS status: {result.get('fbs_status', 'not shown')}"
    )
    try:
        await context.bot.send_photo(
            chat_id=query.from_user.id,
            photo=result["receipt_png"],
            caption=caption[:1024],
            parse_mode=None,
        )
        return
    except Exception as error:
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
    except Exception as error:
        logger.warning("Receipt delivery unavailable (%s)", type(error).__name__)
        await query.message.reply_text(
            "Your booking is confirmed, but I couldn't deliver its screenshot. "
            "Open my private chat and send /start to allow private receipts. "
            "You can view this reference in FBS My Bookings; do not book again."
        )


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
            progress, auth_notify=owner_auth_notifier(context.bot)
        )
    except FBSError as error:
        await status.edit_text(f"⚠️ Could not check Task List: {error}")
        return
    if not result["total"]:
        await status.edit_text(
            "✅ Your FBS Task List is empty. There are no items awaiting your action."
        )
        return
    await status.edit_text(
        f"📋 FBS Task List: {result['total']} item(s).\n"
        "Review the request type and status below. Open FBS Task List to approve or reject a request."
    )
    chunk = ""
    for task in result["tasks"]:
        entry = (
            f"Reference: {task['reference_number'][:120]}\n"
            f"Room: {task['room'][:200]}\n"
            f"When: {task['date_time'][:250]}\n"
            f"Request: {task['request_type'][:200]}\n"
            f"Requested by: {task['requested_by'][:200]}\n"
            f"Status: {task['status'][:200]}\n\n"
        )
        if len(chunk) + len(entry) > 3800:
            await message.reply_text(chunk, parse_mode=None)
            chunk = ""
        chunk += entry
    if not result["complete"]:
        chunk += f"Showing {len(result['tasks'])} of {result['total']} tasks. Open FBS Task List for the remaining items."
    if chunk:
        await message.reply_text(chunk, parse_mode=None)


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
        await query.answer("No active scrape", show_alert=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = context.user_data.pop("booking_wizard", None)
    drafts = context.user_data.pop("booking_drafts", None)
    state = context.user_data.pop("settings_state", None)
    await update.message.reply_text(
        "Cancelled the current questions and pending booking reviews. No booking was submitted."
        if active or drafts or state
        else "There’s nothing waiting to be cancelled."
    )


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "selected_room",
        "last_search",
        "last_room_order",
        "booking_drafts",
        "booking_wizard",
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
        "2. /start — search FBS\n"
        "3. Select a room or auto-pick\n"
        "4. Answer the purpose and co-booker questions\n"
        "5. Validate, then explicitly confirm the booking\n\n"
        "/book — start booking questions for the selected room\n"
        "/cobookers — save names/emails or remove saved co-bookers\n"
        "/tasks — check your FBS Task List for requests awaiting action\n"
        "/cancel — stop questions and discard pending booking reviews\n\n"
        "Your SMU session stays in a private browser profile; approve MFA on your phone when prompted.",
        parse_mode=ParseMode.HTML,
    )


PERSISTENCE_PATH = os.path.join(
    os.path.dirname(__file__), "bot_state.pickle"
)  


async def telegram_error(update, context):
    from telegram.error import Conflict, InvalidToken

    if isinstance(context.error, (Conflict, InvalidToken)):
        logger.error(
            "Telegram rejected polling. Check BOT_TOKEN and stop any other instance of this bot."
        )
        context.application.stop_running()
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
        .post_init(post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.ALL, access_guard), group=-1)
    app.add_handler(CallbackQueryHandler(access_guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("config", scrape_config_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("book", book_command))
    app.add_handler(CommandHandler("cobookers", cobookers_command))
    app.add_handler(CommandHandler(["tasks", "tasklist"], tasks_command))
    app.add_handler(CallbackQueryHandler(wizard_callback, pattern=r"^wizard:"))
    app.add_handler(
        CallbackQueryHandler(scrape_config_callback, pattern=r"^(pick|set|toggle):")
    )
    app.add_handler(CallbackQueryHandler(room_details_callback, pattern=r"^room:"))
    app.add_handler(
        CallbackQueryHandler(select_room_callback, pattern=r"^select_room:")
    )
    app.add_handler(CallbackQueryHandler(autopick_callback, pattern=r"^autopick$"))
    app.add_handler(
        CallbackQueryHandler(
            booking_action_callback,
            pattern=r"^booking_(validate|confirm|cancel):",
        )
    )
    app.add_handler(
        CallbackQueryHandler(scrape_cancel_callback, pattern=r"^scrape_cancel$")
    )
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_error_handler(telegram_error)
    return app


def main():
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bot.cli import main as cli_main

    return cli_main(sys.argv[1:] or ["run"])


if __name__ == "__main__":
    raise SystemExit(main())
