import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─── CONFIGURAÇÃO ─────────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN")  # Vai ficar salvo no Railway como variável de ambiente

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── COMANDOS ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *ShotAlert Bot online!*\n\n"
        "Comandos disponíveis:\n"
        "/start — Iniciar o bot\n"
        "/ping — Testar se o bot está vivo\n"
        "/alerta — Ver alertas de hoje (em breve)\n",
        parse_mode="Markdown"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot online e funcionando!")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))

    print("Bot rodando...")
    app.run_polling()
