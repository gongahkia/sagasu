import json
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
from scraper import do

def read_token(token_filepath):
    try:
        with open(token_filepath, 'r') as file:
            data = json.load(file)
        return data
    except FileNotFoundError:
        print("File not found. Please check the file path.")
    except json.JSONDecodeError:
        print("Error decoding JSON. Please check the file format.")

def run_script(update: Update, context: CallbackContext):

    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"

    result = do.scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH)

    update.message.reply_text(result)

def main():
    updater = Updater(read_token("YOUR_BOT_API_TOKEN")["token"])
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("run_script", run_script))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()