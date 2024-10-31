import json
from telegram.constants import ParseMode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from .async_do import scrape_smu_fbs 
from .async_do import fill_missing_timeslots

def read_token(token_filepath):
    try:
        with open(token_filepath, 'r') as file:
            data = json.load(file)
        return data["bot_token"]
    except FileNotFoundError:
        print("File not found. Please check the file path.")
        return None
    except json.JSONDecodeError:
        print("Error decoding JSON. Please check the file format.")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Run Scraping Script 🤯", callback_data='run_script')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Ello! Click the button to run the script 👋', reply_markup=reply_markup)

async def run_script(callback_query: Update, context: ContextTypes.DEFAULT_TYPE):
    await callback_query.answer()  
    print("Running the scraping script...")
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"
    try:
        result = await scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH)  

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
            parse_mode=ParseMode.HTML
        )

        print("Scraping completed successfully.")
        
        if len(result_errors) > 0:
            response_text = ""
            response_text = "\n".join(result_errors)
            max_length = 4096 
            for i in range(0, len(response_text), max_length):
                await callback_query.message.reply_text(response_text[i:i + max_length])
        else:
            for room, bookings in scraped_results.items():
                complete_bookings = fill_missing_timeslots(bookings)
                response_text = ""
                response_text += f"<code>{room}</code> 🏠\n\n"
                for booking in complete_bookings:
                    if booking["available"]:
                        response_text += f"<i>Timeslot:</i> {booking['timeslot']}\n"
                        response_text += f"<i>Status:</i> <u><a href='https://fbs.intranet.smu.edu.sg/home'>Available to book</a></u> ✅\n\n"
                    else:
                        if booking["details"]:
                            response_text += f"<i>Timeslot:</i> {booking['timeslot']}\n"
                            response_text += f"<i>Status:</i> Booked ❌\n"
                            response_text += f"<i>Purpose:</i> {booking['details']['Purpose of Booking']}\n"
                            response_text += f"<i>Booker:</i> {booking['details']['Booked for User Name']} ({booking['details']['Booked for User Email Address']})\n"
                            response_text += f"<i>Booking ref no:</i> {booking['details']['Booking Reference Number']}\n\n"
                        else:
                            response_text += f"<i>Timeslot:</i> {booking['timeslot']}\n"
                            response_text += f"<i>Status:</i> Outside hours and cannot be booked 🔒\n\n"
                await callback_query.message.reply_text(response_text, parse_mode=ParseMode.HTML)
            await callback_query.message.reply_text("<b><i>All results have been displayed! 🥳</i></b>", parse_mode=ParseMode.HTML)

    except Exception as e:
        print(f"Error during scraping: {e}")
        await callback_query.message.reply_text("An error occurred during the scraping process. Report the issue @gongahkia.")
        
    finally:
        if context.bot_data.get('browser'):  
            await context.bot_data['browser'].close()
            print("Browser closed.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 
    if query.data == 'run_script':
        new_keyboard = [[InlineKeyboardButton("Oke the script is running 🏃...", callback_data='disabled')]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
        try:
            await run_script(query, context) 
        except Exception as e:
            print(f"Error during scraping: {e}")
            try:
                await query.edit_message_text("An error occurred during the scraping process.")
            except Exception as edit_error:
                print(f"Failed to edit message: {edit_error}")

def main():
    app = ApplicationBuilder().token(read_token("token.json")).build()
    app.add_handler(CommandHandler("start", start)) 
    app.add_handler(CallbackQueryHandler(button_callback)) 
    print("Bot is polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
