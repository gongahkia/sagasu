import json
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from scraper import do

def read_token(token_filepath):
    try:
        with open(token_filepath, 'r') as file:
            data = json.load(file)
        return data["token"]
    except FileNotFoundError:
        print("File not found. Please check the file path.")
        return None
    except json.JSONDecodeError:
        print("Error decoding JSON. Please check the file format.")
        return None

async def run_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("running the scraping script...")
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"
    result = do.scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH)
    await update.message.reply_text(result)

def main():
    token = read_token("token.json")
    if token is None:
        print("Failed to retrieve token. Exiting...")
        return
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("run_script", run_script))
    print("polling...")
    app.run_polling()

if __name__ == "__main__":
    # main()
    TARGET_URL = "https://fbs.intranet.smu.edu.sg/home"
    CREDENTIALS_FILEPATH = "credentials.json"
    print(do.scrape_smu_fbs(TARGET_URL, CREDENTIALS_FILEPATH))