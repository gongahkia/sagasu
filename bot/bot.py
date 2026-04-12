import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram.constants import ParseMode
from telegram.ext import MessageHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PicklePersistence,
)
from async_do import scrape_smu_fbs
from async_do import fill_missing_timeslots
from async_do import AuthFailedError, NetworkError, FBSLayoutError, ScrapeError

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Poke to start scraping 🤯", callback_data="run_script")],
        [
            InlineKeyboardButton(
                "Pinch to alert help desk 📖", callback_data="view_help"
            )
        ],
        [InlineKeyboardButton("Tickle to open settings ⚙️", callback_data="settings")],
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
    await callback_query.answer()
    print("Running the scraping script...")
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    # CREDENTIALS_FILEPATH = "credentials.json"

    USER_EMAIL = context.user_data.get("email")
    USER_PASSWORD = context.user_data.get("password")

    # print(USER_EMAIL, USER_PASSWORD)
    # if not USER_EMAIL:
    #     await callback_query.message.reply_text("Email not provided lah! Go set it in settings. 💀")
    #     return
    # elif not USER_PASSWORD:
    #     await callback_query.message.reply_text("Password is missing leh! Go set it in settings. 🤡")
    #     return

    try:
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel", callback_data="scrape_cancel")]])
        status_msg = await callback_query.message.reply_text("⏳ Starting scrape…", reply_markup=cancel_kb)

        async def progress(stage):
            try:
                await status_msg.edit_text(f"⏳ {stage}…", reply_markup=cancel_kb)
            except Exception as e:
                print(f"progress edit failed: {e}")

        scrape_task = asyncio.create_task(
            scrape_smu_fbs(TARGET_URL, USER_EMAIL, USER_PASSWORD, context.user_data.get("scrape_config"), progress)
        )
        context.user_data["scrape_task"] = scrape_task
        try:
            result = await scrape_task
        except asyncio.CancelledError:
            await status_msg.edit_text("🛑 Scrape cancelled.")
            return
        finally:
            context.user_data.pop("scrape_task", None)

        result_errors = result[0]
        result_final_booking_log = result[1]
        metrics = result_final_booking_log["metrics"]
        scraped_configuration = result_final_booking_log["scraped"]["config"]
        scraped_results = result_final_booking_log["scraped"]["result"]

        # ----- REPLY THE USER -----

        await callback_query.message.reply_text(
            f"Scraping carried out on at <b>{metrics['scraping_date']} ⏲️</b>\n\n"
            f"<b>Your scraping configuration ⚙️</b>\n"
            f"<i>Target date:</i> {scraped_configuration['date']}\n"
            f"<i>Target start time:</i> {scraped_configuration['start_time']}\n"
            f"<i>Target end time:</i> {scraped_configuration['end_time']}\n"
            f"<i>Target duration:</i> {scraped_configuration['duration']}\n"
            f"<i>Target buildings:</i> {', '.join(scraped_configuration['building_names'])}\n"
            f"<i>Target floors:</i> {', '.join(scraped_configuration['floors'])}\n"
            f"<i>Target facility types:</i> {', '.join(scraped_configuration['facility_types'])}\n"
            f"<i>Target room capacity:</i> {scraped_configuration['room_capacity']}\n"
            f"<i>Target equipment:</i> {', '.join(scraped_configuration['equipment'])}",
            parse_mode=ParseMode.HTML,
        )

        print("Scraping completed successfully.")

        if len(result_errors) > 0:
            response_text = ""
            response_text = "\n".join(result_errors)
            max_length = 4096
            for i in range(0, len(response_text), max_length):
                await callback_query.message.reply_text(
                    response_text[i : i + max_length]
                )
        else:
            context.user_data["last_results"] = {}
            rooms_with_availability = []
            for room, bookings in scraped_results.items():
                complete_bookings = fill_missing_timeslots(bookings)
                context.user_data["last_results"][room] = complete_bookings
                available_slots = [b["timeslot"] for b in complete_bookings if b["available"]]
                if available_slots:
                    rooms_with_availability.append((room, available_slots))

            if not rooms_with_availability:
                await callback_query.message.reply_text(
                    "😴 No rooms with availability in your window.\nAdjust /config and try again."
                )
            else:
                summary = f"<b>🥳 {len(rooms_with_availability)} room(s) with openings:</b>\n\n"
                buttons = []
                for idx, (room, slots) in enumerate(rooms_with_availability):
                    summary += f"• <code>{room}</code> — {len(slots)} slot(s): {', '.join(slots[:3])}{'…' if len(slots) > 3 else ''}\n"
                    buttons.append([InlineKeyboardButton(f"🔍 {room}", callback_data=f"room:{idx}")])
                context.user_data["last_room_order"] = [r for r, _ in rooms_with_availability]
                await callback_query.message.reply_text(
                    summary, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons)
                )

    except AuthFailedError as e:
        print(f"Auth failed: {e}")
        await callback_query.message.reply_text(
            "🔐 Login rejected. Check your SMU email and password via /settings."
        )
    except NetworkError as e:
        print(f"Network error: {e}")
        await callback_query.message.reply_text(
            "🌐 Couldn't reach FBS. Check your connection — if it persists, FBS may be down."
        )
    except FBSLayoutError as e:
        print(f"FBS layout changed: {e}")
        await callback_query.message.reply_text(
            "🧩 FBS page structure changed — the scraper needs an update. Report @gongahkia."
        )
    except ScrapeError as e:
        print(f"Scrape error: {e}")
        await callback_query.message.reply_text(f"⚠️ Scrape failed: {e}")
    except Exception as e:
        print(f"Error during scraping: {e}")
        await callback_query.message.reply_text(
            "An error occurred during the scraping process. Report the issue @gongahkia."
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "run_script":

        USER_EMAIL = context.user_data.get("email")
        USER_PASSWORD = context.user_data.get("password")

        # print(USER_EMAIL, USER_PASSWORD)

        if not USER_EMAIL:
            await query.message.reply_text(
                "Email not provided lah! Go set it in settings. 💀"
            )
            return
        elif not USER_PASSWORD:
            await query.message.reply_text(
                "Password is missing leh! Go set it in settings. 🤡"
            )
            return
        else:
            await query.message.reply_text(
                "Email and Password found! Initiating scraping... 👌"
            )

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
    elif query.data == "view_help":
        await query.edit_message_text(
            "<code>Sagasu</code> scrapes SMU FBS data.\n\nType /start to see all options\nType /help for help\nType /settings to adjust your credentials\nType /config to adjust scrape params\nType /cancel to abort an input flow\nType /logout to wipe stored credentials",
            parse_mode=ParseMode.HTML,
        )
    elif query.data == "settings":
        await query.message.reply_text("Please enter your SMU email address 📧")
        context.user_data["settings_state"] = "awaiting_email"


SMU_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@(smu\.edu\.sg|(scis|sis|law|business|accountancy|economics|socsc)\.smu\.edu\.sg)$", re.IGNORECASE)


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("settings_state") == "awaiting_email":
        email = update.message.text.strip()
        if not SMU_EMAIL_RE.match(email):
            await update.message.reply_text(
                "That doesn't look like a valid SMU email 🤔\nTry again, or /cancel to abort."
            )
            return
        context.user_data["email"] = email
        await update.message.reply_text(
            "SMU email saved!\nPlease enter your password 🔑\n\n⚠️ Your password message will be deleted immediately after I read it to protect your chat history."
        )
        context.user_data["settings_state"] = "awaiting_password"
    else:
        await update.message.reply_text(
            "Don't cut queue lah you.\nType /settings to enter your email 🐻‍❄️."
        )


async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("settings_state") == "awaiting_password":
        context.user_data["password"] = update.message.text
        try:
            await update.message.delete()  # purge plaintext password from chat history
        except Exception as e:
            print(f"Could not delete password message: {e}")
        await update.message.chat.send_message("Password saved and your message was deleted for safety!\nSettings updated ☑️")
        context.user_data["settings_state"] = None
    else:
        await update.message.reply_text(
            "Don't cut queue lah you.\nType /settings to enter your password 🐻."
        )


SCRAPE_PARAM_CHOICES = {
    "date": [
        ("Today", 0), ("Tomorrow", 1), ("+2 days", 2), ("+3 days", 3),
        ("+4 days", 4), ("+5 days", 5), ("+6 days", 6),
    ],
    "start_time": ["08:00", "09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00"],
    "duration": [0.5, 1, 1.5, 2, 2.5, 3, 4],
    "capacity": [
        ("Any", None), ("<5 pax", "LessThan5Pax"), ("6-10 pax", "From6To10Pax"),
        ("11-15 pax", "From11To15Pax"), ("16-20 pax", "From16To20Pax"),
        ("21-50 pax", "From21To50Pax"), (">50 pax", "From51To100Pax"),
    ],
    "building": [
        "School of Computing & Information Systems 1",
        "School of Computing & Information Systems 2",
        "School of Accountancy",
        "Yong Pung How School of Law/Kwa Geok Choo Law Library",
        "School of Economics/School of Social Sciences",
        "Lee Kong Chian School of Business",
        "Li Ka Shing Library",
        "Administration Building",
    ],
    "floor": ["Basement 1", "Level 1", "Level 2", "Level 3", "Level 4", "Level 5"],
    "facility_type": ["Group Study Room", "Project Room", "Meeting Room", "Seminar Room", "Classroom"],
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
        [InlineKeyboardButton(f"⏰ Start: {label('start_time', '11:00')}", callback_data="pick:start_time")],
        [InlineKeyboardButton(f"⏳ Duration: {label('duration_hrs', 2.5)}h", callback_data="pick:duration")],
        [InlineKeyboardButton(f"👥 Capacity: {label('room_capacity', 'Any')}", callback_data="pick:capacity")],
        [InlineKeyboardButton(f"🏢 Buildings: {len(cfg.get('buildings') or [])} selected", callback_data="pick:building")],
        [InlineKeyboardButton(f"🪜 Floors: {len(cfg.get('floors') or [])} selected", callback_data="pick:floor")],
        [InlineKeyboardButton(f"🛋️ Facility: {len(cfg.get('facility_types') or [])} selected", callback_data="pick:facility_type")],
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
        buttons = [[InlineKeyboardButton(lbl, callback_data=f"set:date:{off}")] for lbl, off in SCRAPE_PARAM_CHOICES["date"]]
    elif param == "start_time":
        buttons = [[InlineKeyboardButton(t, callback_data=f"set:start_time:{t}")] for t in SCRAPE_PARAM_CHOICES["start_time"]]
    elif param == "duration":
        buttons = [[InlineKeyboardButton(f"{d}h", callback_data=f"set:duration:{d}")] for d in SCRAPE_PARAM_CHOICES["duration"]]
    elif param == "capacity":
        buttons = [[InlineKeyboardButton(lbl, callback_data=f"set:capacity:{val or 'ANY'}")] for lbl, val in SCRAPE_PARAM_CHOICES["capacity"]]
    elif param in ("building", "floor", "facility_type"):
        key_map = {"building": "buildings", "floor": "floors", "facility_type": "facility_types"}
        selected = set(cfg.get(key_map[param]) or [])
        buttons = []
        for i, opt in enumerate(SCRAPE_PARAM_CHOICES[param]):
            mark = "✅ " if opt in selected else "◻️ "
            buttons.append([InlineKeyboardButton(f"{mark}{opt}", callback_data=f"toggle:{param}:{i}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="pick:done")])
    else:
        return
    if param in ("date", "start_time", "duration", "capacity"):
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="pick:done")])
    await query.edit_message_text(f"Select {param}:", reply_markup=InlineKeyboardMarkup(buttons))


async def scrape_config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    cfg = get_scrape_config(context)
    if data.startswith("pick:"):
        param = data.split(":", 1)[1]
        if param == "done":
            await query.edit_message_text("⚙️ Scrape configuration — tap a row to change:", reply_markup=_build_scrape_menu_keyboard(context))
            return
        if param == "reset":
            context.user_data["scrape_config"] = {}
            await query.edit_message_text("⚙️ Scrape configuration — tap a row to change:", reply_markup=_build_scrape_menu_keyboard(context))
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
        await query.edit_message_text("⚙️ Scrape configuration — tap a row to change:", reply_markup=_build_scrape_menu_keyboard(context))
    elif data.startswith("toggle:"):
        _, param, idx_s = data.split(":", 2)
        idx = int(idx_s)
        key_map = {"building": "buildings", "floor": "floors", "facility_type": "facility_types"}
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
    state = context.user_data.get("settings_state")
    if state == "awaiting_email":
        await handle_email(update, context)
    elif state == "awaiting_password":
        await handle_password(update, context)
    else:
        await update.message.reply_text(
            "Quit yapping bruh, I'm not expecting any input right now.\nType /settings to configure. 🦜"
        )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    has_email = bool(context.user_data.get("email"))
    has_password = bool(context.user_data.get("password"))
    if has_email and has_password:
        masked = _mask_email(context.user_data["email"])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Change email", callback_data="settings:edit_email")],
            [InlineKeyboardButton("🔑 Change password", callback_data="settings:edit_password")],
            [InlineKeyboardButton("❌ Cancel", callback_data="settings:cancel")],
        ])
        await update.message.reply_text(
            f"Stored email: {masked}\nPick a field to change:", reply_markup=kb
        )
        return
    context.user_data["settings_state"] = "awaiting_email"
    await update.message.reply_text("Please enter your SMU email address 📧\n(or /cancel to abort)")


def _mask_email(email: str) -> str:
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        return "*" * len(local) + "@" + domain
    return local[0] + "***" + local[-1] + "@" + domain


async def settings_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "edit_email":
        context.user_data["settings_state"] = "awaiting_email"
        await query.edit_message_text("Please enter your new SMU email address 📧\n(or /cancel to abort)")
    elif action == "edit_password":
        context.user_data["settings_state"] = "awaiting_password"
        await query.edit_message_text(
            "Please enter your new password 🔑\n\n⚠️ Your message will be deleted immediately after I read it.\n(or /cancel to abort)"
        )
    elif action == "cancel":
        await query.edit_message_text("Settings unchanged 👍")


async def room_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":", 1)[1])
    order = context.user_data.get("last_room_order") or []
    if idx >= len(order):
        await query.message.reply_text("Results expired — run a new scrape.")
        return
    room = order[idx]
    bookings = (context.user_data.get("last_results") or {}).get(room, [])
    text = f"<code>{room}</code> 🏠\n\n"
    for b in bookings:
        if b["available"]:
            text += f"<i>{b['timeslot']}</i> — <u><a href='https://fbs.intranet.smu.edu.sg/home'>Available</a></u> ✅\n"
        elif b["details"]:
            text += f"<i>{b['timeslot']}</i> — Booked ❌ ({b['details'].get('Purpose of Booking', 'n/a')})\n"
        else:
            text += f"<i>{b['timeslot']}</i> — Outside hours 🔒\n"
    max_len = 4096
    for i in range(0, len(text), max_len):
        await query.message.reply_text(text[i:i+max_len], parse_mode=ParseMode.HTML)


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
    wiped = []
    for key in ("email", "password", "settings_state"):
        if key in context.user_data:
            context.user_data.pop(key, None)
            wiped.append(key)
    if wiped:
        await update.message.reply_text(
            "All your stored credentials have been wiped clean 🧹\nType /settings to re-enter them."
        )
    else:
        await update.message.reply_text("Nothing to wipe — no credentials were stored 👻")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<code>Sagasu</code> scrapes SMU FBS data.\n\nType /start to see all options\nType /help for help\nType /settings to adjust your credentials\nType /config to adjust scrape params\nType /cancel to abort an input flow\nType /logout to wipe stored credentials",
        parse_mode=ParseMode.HTML,
    )


PERSISTENCE_PATH = os.path.join(os.path.dirname(__file__), "bot_state.pickle")  # user_data survives restarts


def main():
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    app = ApplicationBuilder().token(read_token_env()).persistence(persistence).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("config", scrape_config_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(scrape_config_callback, pattern=r"^(pick|set|toggle):"))
    app.add_handler(CallbackQueryHandler(settings_edit_callback, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(room_details_callback, pattern=r"^room:"))
    app.add_handler(CallbackQueryHandler(scrape_cancel_callback, pattern=r"^scrape_cancel$"))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT, handle_text_input))
    print("Bot is polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
