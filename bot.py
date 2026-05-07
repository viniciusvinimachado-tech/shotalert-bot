import os
import logging
from telegram.ext import Updater, CommandHandler

TOKEN = os.environ.get("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

def start(update, context):
    update.message.reply_text(
        "🤖 ShotAlert Bot online!\n\n"
        "/ping — Testar bot"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot online e funcionando!")

def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("ping", ping))
    print("Bot rodando...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
