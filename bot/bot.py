import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from .async_do import scrape_smu_fbs 

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
    await update.message.reply_text('Welcome! Click the button to run the script:', reply_markup=reply_markup)

async def run_script(callback_query: Update, context: ContextTypes.DEFAULT_TYPE):
    await callback_query.answer()  
    print("Running the scraping script...")
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"
    try:
        result = await scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH)  

        """
        FUA 
        
        figure out the section here for how the result can be displayed 
        """

        result_errors = result[0]
        result_final_booking_log = result[1]
        metrics = result_final_booking_log["metrics"]
        scraped_configuration = result_final_booking_log["scraped"]["config"]
        scraped_results = result_final_booking_log["scraped"]["result"]
        response_text = ""
        await callback_query.message.reply_text(metrics)
        await callback_query.message.reply_text(json.dumps(scraped_configuration, indent=2))

        print("Scraping completed successfully.")
        
        if len(result_errors) > 0:
            response_text = "\n- ".join(result_errors)
            max_length = 4096 
            for i in range(0, len(response_text), max_length):
                await callback_query.message.reply_text(response_text[i:i + max_length])
        else:
            for room, bookings in scraped_results.items():
                response_text += f"`***{room}***`\n"
                for booking in bookings:
                    await callback_query.message.reply_text(json.dumps(booking, indent=2))
                # FUA to debug the below!!!
                #     status = "Booked" if not booking['available'] else "Available"
                #     response_text += f"\tTimeslot: {booking['timeslot']}\n\tStatus: {status}\n"
                #     if booking['details']:
                #         details = booking['details']
                #         response_text += (f"\tPurpose: {details['Purpose of Booking']}\n\tBooking Reference: {details['Booking Reference Number']}\n\tBooked for: {details['Booked for User Name']} ({details['Booked for User Email Address']})")
                # response_text += "\n"
                # max_length = 4096 
                # for i in range(0, len(result), max_length):
                #     await callback_query.message.reply_text(result_final_booking_log[i:i + max_length])

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
            await run_script(query, context)  # Pass the query object
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
