import os
import logging
import requests
from telegram.ext import Updater, CommandHandler

TOKEN = os.environ.get("BOT_TOKEN")
API_KEY = os.environ.get("API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

HEADERS = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
}

# ─── BUSCA JOGOS DO DIA ───────────────────────────────────────────────────────

def buscar_jogos():
    from datetime import date
    hoje = date.today().strftime("%Y-%m-%d")
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"date": hoje, "league": "71", "season": "2025"}  # Liga 71 = Brasileirão
    resp = requests.get(url, headers=HEADERS, params=params)
    return resp.json().get("response", [])

# ─── BUSCA ESTATÍSTICAS DE FINALIZAÇÕES ───────────────────────────────────────

def buscar_stats(team_id):
    url = "https://api-football-v1.p.rapidapi.com/v3/teams/statistics"
    params = {"team": team_id, "league": "71", "season": "2025"}
    resp = requests.get(url, headers=HEADERS, params=params)
    data = resp.json().get("response", {})
    shots = data.get("shots", {})
    jogos = data.get("fixtures", {}).get("played", {}).get("total", 1)
    total_chutes = shots.get("total", {}).get("total", 0)
    chutes_no_alvo = shots.get("on", {}).get("total", 0)
    if jogos and jogos > 0:
        media = round(total_chutes / jogos, 1)
        media_alvo = round(chutes_no_alvo / jogos, 1)
    else:
        media = 0
        media_alvo = 0
    return media, media_alvo

# ─── COMANDOS ─────────────────────────────────────────────────────────────────

def start(update, context):
    update.message.reply_text(
        "🤖 *Finalizações Bot online!*\n\n"
        "Comandos disponíveis:\n"
        "/jogos — Ver jogos do dia\n"
        "/alerta — Ver melhores entradas\n"
        "/ping — Testar bot",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot online e funcionando!")

def jogos(update, context):
    update.message.reply_text("🔍 Buscando jogos do Brasileirão hoje...")
    try:
        fixtures = buscar_jogos()
        if not fixtures:
            update.message.reply_text("📭 Nenhum jogo do Brasileirão hoje.")
            return
        msg = "⚽ *Jogos de hoje — Brasileirão:*\n\n"
        for f in fixtures[:5]:
            casa = f["teams"]["home"]["name"]
            fora = f["teams"]["away"]["name"]
            hora = f["fixture"]["date"][11:16]
            msg += f"🕐 {hora} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        update.message.reply_text(f"❌ Erro ao buscar jogos: {e}")

def alerta(update, context):
    update.message.reply_text("📊 Analisando finalizações...")
    try:
        fixtures = buscar_jogos()
        if not fixtures:
            update.message.reply_text("📭 Nenhum jogo encontrado hoje.")
            return
        alertas = []
        for f in fixtures[:3]:
            casa = f["teams"]["home"]["name"]
            fora = f["teams"]["away"]["name"]
            id_casa = f["teams"]["home"]["id"]
            id_fora = f["teams"]["away"]["id"]
            hora = f["fixture"]["date"][11:16]
            media_casa, alvo_casa = buscar_stats(id_casa)
            media_fora, alvo_fora = buscar_stats(id_fora)
            total = media_casa + media_fora
            if total >= 20:
                alertas.append({
                    "casa": casa,
                    "fora": fora,
                    "hora": hora,
                    "total": total,
                    "media_casa": media_casa,
                    "media_fora": media_fora,
                })
        if not alertas:
            update.message.reply_text("⚠️ Nenhum jogo com média alta hoje.")
            return
        for a in alertas:
            msg = (
                f"🚨 *ALERTA DE FINALIZAÇÕES*\n\n"
                f"⚽ {a['casa']} x {a['fora']}\n"
                f"🕐 {a['hora']}\n\n"
                f"📊 Média combinada: *{a['total']} fin/jogo*\n"
                f"🏠 {a['casa']}: {a['media_casa']} fin/jogo\n"
                f"✈️ {a['fora']}: {a['media_fora']} fin/jogo\n\n"
                f"💰 Mercado sugerido: Ambos >20.5 finalizações\n"
                f"🏦 Verificar: Bet365 / Betano / Superbet"
            )
            update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        update.message.reply_text(f"❌ Erro na análise: {e}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("jogos", jogos))
    dp.add_handler(CommandHandler("alerta", alerta))
    print("Bot rodando...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
