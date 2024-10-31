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
    print("Running the scraping script...")
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"
    try:
        result = await scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH)
        print("Scraping completed successfully.")
        await callback_query.answer()  # Acknowledge the button press
        await callback_query.callback_query.message.reply_text(result)
    except Exception as e:
        print(f"Error during scraping: {e}")
        await callback_query.callback_query.answer("An error occurred during the scraping process.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'run_script':
        new_keyboard = [[InlineKeyboardButton("Oke, the script is running 🏃...", callback_data='disabled')]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
        
        try:
            await run_script(query, context)  # Correct, passing query directly
        except Exception as e:
            print(f"Error during scraping: {e}")
            await query.edit_message_text("An error occurred during the scraping process.")

def main():
    app = ApplicationBuilder().token(read_token("token.json")).build()
    app.add_handler(CommandHandler("start", start)) 
    app.add_handler(CallbackQueryHandler(button_callback)) 
    print("Bot is polling...")
    app.run_polling()

if __name__ == "__main__":
    main()