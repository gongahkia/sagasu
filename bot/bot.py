import asyncio
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from html import escape
from dotenv import load_dotenv
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
    from .fbs_client import (
        AuthenticationRequired,
        BookingRequest,
        BookingUnavailable,
        FBSError,
        LayoutChanged,
        SearchConfig,
        execute_booking,
        search_config_from_bot,
        search_fbs,
    )
except ImportError:
    if __package__:
        raise
    from fbs_client import (
        AuthenticationRequired,
        BookingRequest,
        BookingUnavailable,
        FBSError,
        LayoutChanged,
        SearchConfig,
        execute_booking,
        search_config_from_bot,
        search_fbs,
    )


def read_token_env():
    """
    read bot token from a .env file
    """
    load_dotenv()
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        print("One or more credentials are missing in the .env file")
        return None
    else:
        return bot_token


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restrict a deployed personal bot when TELEGRAM_OWNER_ID is configured."""

    owner_id = os.getenv("TELEGRAM_OWNER_ID")
    if not owner_id or (update.effective_user and str(update.effective_user.id) == owner_id):
        return
    if update.callback_query:
        await update.callback_query.answer("This is a private Sagasu instance.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("This is a private Sagasu instance.")
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔎 Find available rooms", callback_data="run_script")],
        [
            InlineKeyboardButton(
                "Pinch to alert help desk 📖", callback_data="view_help"
            )
        ],
        [InlineKeyboardButton("⚙️ Search settings", callback_data="open_config")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        'Welcome to <a href="https://github.com/gongahkia/sagasu">Sagasu</a>!',
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text(
        "Ello! Click one option below 👋", reply_markup=reply_markup
    )


async def run_script(callback_query: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Running the scraping script...")

    try:
        cancel_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 Cancel", callback_data="scrape_cancel")]]
        )
        status_msg = await callback_query.message.reply_text(
            "⏳ Starting scrape…", reply_markup=cancel_kb
        )

        async def progress(stage):
            try:
                await status_msg.edit_text(f"⏳ {stage}…", reply_markup=cancel_kb)
            except Exception as e:
                print(f"progress edit failed: {e}")

        search_config = search_config_from_bot(
            context.user_data.get("scrape_config") or {}
        )
        scrape_task = asyncio.create_task(search_fbs(search_config, progress))
        context.user_data["scrape_task"] = scrape_task
        try:
            result = await scrape_task
        except asyncio.CancelledError:
            await status_msg.edit_text("🛑 Scrape cancelled.")
            return
        finally:
            context.user_data.pop("scrape_task", None)

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
                [InlineKeyboardButton("✨ Auto-pick best room", callback_data="autopick")]
            ]
            for idx, room in enumerate(rooms[:20]):
                summary += f"• <code>{escape(room.name)}</code>\n"
                buttons.append(
                    [InlineKeyboardButton(f"🏠 {room.name}", callback_data=f"room:{idx}")]
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
    except Exception as e:
        print(f"Error during scraping: {e}")
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
            print(f"Error during scraping: {e}")
            try:
                await query.edit_message_text(
                    "An error occurred during the scraping process. 🌋"
                )
            except Exception as edit_error:
                print(f"Failed to edit message: {edit_error}")
    elif query.data == "open_config":
        await query.message.reply_text(
            "⚙️ Search configuration — tap a row to change:",
            reply_markup=_build_scrape_menu_keyboard(context),
        )
    elif query.data == "view_help":
        await query.edit_message_text(
            "<code>Sagasu</code> finds and books SMU rooms.\n\n"
            "Type /config to set search filters\n"
            "Type /start to search\n"
            "Select or auto-pick a room\n"
            "Type /book purpose | co-booker email\n"
            "Type /status to view current settings",
            parse_mode=ParseMode.HTML,
        )
    elif query.data == "settings":
        await query.message.reply_text(
            "Sagasu now uses a private local Chrome profile. No SMU password is "
            "stored in Telegram. Run a search and complete MFA in Chrome when prompted."
        )


SMU_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@(smu\.edu\.sg|(scis|sis|law|business|accountancy|economics|socsc)\.smu\.edu\.sg)$",
    re.IGNORECASE,
)


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
        "08:00",
        "09:00",
        "10:00",
        "11:00",
        "12:00",
        "13:00",
        "14:00",
        "15:00",
        "16:00",
        "17:00",
        "18:00",
        "19:00",
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

    date_raw = cfg.get("date_raw", "Today (default)")
    kb = [
        [InlineKeyboardButton(f"📅 Date: {date_raw}", callback_data="pick:date")],
        [
            InlineKeyboardButton(
                f"⏰ Start: {label('start_time', '11:00')}",
                callback_data="pick:start_time",
            )
        ],
        [
            InlineKeyboardButton(
                f"⏳ Duration: {label('duration_hrs', 2.5)}h",
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


async def scrape_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ Scrape configuration — tap a row to change:",
        reply_markup=_build_scrape_menu_keyboard(context),
    )


async def _open_param_picker(query, context, param):
    cfg = get_scrape_config(context)
    if param == "date":
        buttons = [
            [InlineKeyboardButton(lbl, callback_data=f"set:date:{off}")]
            for lbl, off in SCRAPE_PARAM_CHOICES["date"]
        ]
    elif param == "start_time":
        buttons = [
            [InlineKeyboardButton(t, callback_data=f"set:start_time:{t}")]
            for t in SCRAPE_PARAM_CHOICES["start_time"]
        ]
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
                "⚙️ Scrape configuration — tap a row to change:",
                reply_markup=_build_scrape_menu_keyboard(context),
            )
            return
        if param == "reset":
            context.user_data["scrape_config"] = {}
            await query.edit_message_text(
                "⚙️ Scrape configuration — tap a row to change:",
                reply_markup=_build_scrape_menu_keyboard(context),
            )
            return
        await _open_param_picker(query, context, param)
    elif data.startswith("set:"):
        _, param, value = data.split(":", 2)
        if param == "date":
            offset_days = int(value)
            target = datetime.now() + timedelta(days=offset_days)
            cfg["date_raw"] = target.strftime("%-d %B %Y").lower()
        elif param == "start_time":
            cfg["start_time"] = value
            cfg.pop("end_time", None)  # recompute from start+duration
        elif param == "duration":
            cfg["duration_hrs"] = float(value)
            cfg.pop("end_time", None)
        elif param == "capacity":
            cfg["room_capacity"] = None if value == "ANY" else value
        await query.edit_message_text(
            "⚙️ Scrape configuration — tap a row to change:",
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
    await update.message.reply_text(
        "I wasn't expecting free text. Use /config, /start, or /book."
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 Authentication uses a private Chrome profile on this computer.\n"
        "Sagasu does not store your SMU password. When the session expires, "
        "the next search opens Chrome so you can complete Microsoft MFA."
    )


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
    room_data = next(
        (candidate for candidate in search.get("rooms", []) if candidate["name"] == room),
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
        text + "\nUse <code>/book purpose | co-booker email</code> to continue.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Select this room", callback_data=f"select_room:{idx}")]]
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
    await query.message.reply_text(
        f"Selected <code>{escape(order[idx])}</code>.\n"
        "Continue with <code>/book purpose | co-booker email</code>.",
        parse_mode=ParseMode.HTML,
    )


async def autopick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get("last_room_order") or []
    if not order:
        await query.message.reply_text("Results expired — run a new search.")
        return
    context.user_data["selected_room"] = order[0]
    await query.message.reply_text(
        f"✨ Auto-picked <code>{escape(order[0])}</code>.\n"
        "Continue with <code>/book purpose | co-booker email</code>.",
        parse_mode=ParseMode.HTML,
    )


async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    room = context.user_data.get("selected_room")
    search = context.user_data.get("last_search")
    if not room or not search:
        await update.message.reply_text("Run a search and select a room first.")
        return
    raw = " ".join(context.args or [])
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "Usage: /book purpose | co-booker email\n"
            "Example: /book Project meeting | friend@smu.edu.sg"
        )
        return
    purpose, co_booker = parts[:2]
    if not SMU_EMAIL_RE.fullmatch(co_booker):
        await update.message.reply_text(
            "The co-booker must be a valid SMU email address."
        )
        return
    booking_usage = parts[2] if len(parts) > 2 and parts[2] else "Meeting"
    request = BookingRequest(
        search=SearchConfig.from_mapping(search["config"]),
        room=room,
        purpose=purpose,
        co_booker=co_booker,
        booking_usage=booking_usage,
    )
    token = secrets.token_urlsafe(8)
    drafts = context.user_data.setdefault("booking_drafts", {})
    drafts[token] = {
        "request": request.to_dict(),
        "expires_at": time.time() + 600,
    }
    preview = (
        "<b>Review booking</b>\n\n"
        f"Room: <code>{escape(room)}</code>\n"
        f"Date: {escape(request.search.date)}\n"
        f"Time: {escape(request.search.start_time)}–{escape(request.search.end_time)}\n"
        f"Purpose: {escape(request.purpose)}\n"
        f"Usage: {escape(request.booking_usage)}\n"
        f"Co-booker: <code>{escape(request.co_booker)}</code>\n\n"
        "Confirming accepts the FBS acknowledgement and declaration. "
        "Availability will be checked again before submission."
    )
    await update.message.reply_text(
        preview,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🧪 Validate only", callback_data=f"booking_validate:{token}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✅ Confirm & book", callback_data=f"booking_confirm:{token}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data=f"booking_cancel:{token}"
                    )
                ],
            ]
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
        await query.edit_message_text("This booking confirmation expired. Create a new one.")
        return
    if action == "booking_cancel":
        drafts.pop(token, None)
        await query.edit_message_text("Booking cancelled locally; nothing was submitted.")
        return

    submit = action == "booking_confirm"
    status = await query.message.reply_text(
        "⏳ Rechecking availability and preparing the booking…"
    )

    async def progress(stage):
        try:
            await status.edit_text(f"⏳ {stage}…")
        except Exception:
            pass

    try:
        request = BookingRequest.from_mapping(draft["request"])
        result = await execute_booking(request, submit=submit, progress=progress)
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
        else:
            await status.edit_text(
                "🧪 Validation passed through the complete pre-confirmation flow. "
                "No booking was submitted."
            )
    except BookingUnavailable as error:
        drafts.pop(token, None)
        await status.edit_text(f"⚠️ Availability changed: {error}")
    except FBSError as error:
        await status.edit_text(f"⚠️ Booking failed safely before completion: {error}")


async def scrape_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task = context.user_data.get("scrape_task")
    if task and not task.done():
        task.cancel()
        try:
            await query.edit_message_text("🛑 Cancelling scrape…")
        except Exception:
            pass
    else:
        await query.answer("No active scrape", show_alert=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.pop("settings_state", None)
    if state:
        await update.message.reply_text(f"Cancelled ({state}) ✋")
    else:
        await update.message.reply_text("Nothing to cancel 👻")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "selected_room",
        "last_search",
        "last_room_order",
        "booking_drafts",
        # Remove credentials left by versions predating browser-profile auth.
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
    lines.append(f"<i>Date:</i> {cfg.get('date_raw', 'today (default)')}")
    lines.append(f"<i>Start time:</i> {cfg.get('start_time', '11:00 (default)')}")
    lines.append(
        f"<i>Duration:</i> {cfg.get('duration_hrs', '2.5h (default)')}h"
        if "duration_hrs" in cfg
        else "<i>Duration:</i> 2.5h (default)"
    )
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
        "4. /book purpose | co-booker email\n"
        "5. Validate, then explicitly confirm the booking\n\n"
        "Microsoft login and MFA happen only in a private local Chrome profile.",
        parse_mode=ParseMode.HTML,
    )


PERSISTENCE_PATH = os.path.join(
    os.path.dirname(__file__), "bot_state.pickle"
)  # user_data survives restarts


def main():
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    app = ApplicationBuilder().token(read_token_env()).persistence(persistence).build()
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
    app.add_handler(
        CallbackQueryHandler(scrape_config_callback, pattern=r"^(pick|set|toggle):")
    )
    app.add_handler(CallbackQueryHandler(room_details_callback, pattern=r"^room:"))
    app.add_handler(CallbackQueryHandler(select_room_callback, pattern=r"^select_room:"))
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
    app.add_handler(MessageHandler(filters.TEXT, handle_text_input))
    print("Bot is polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
