import os
import logging
import requests
from datetime import date, timedelta
from telegram.ext import Updater, CommandHandler
from telegram import Bot

TOKEN = os.environ.get("BOT_TOKEN")
API_KEY = os.environ.get("API_KEY")
CHAT_ID = os.environ.get("CHAT_ID")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

HEADERS = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
}

LIGAS = {
    "Brasileirao": "71",
    "Copa do Brasil": "73",
    "Libertadores": "13",
}

# ─── FUNÇÕES DE BUSCA ─────────────────────────────────────────────────────────

def buscar_jogos_data(data_str, league_id="71"):
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"date": data_str, "league": league_id, "season": "2025"}
    resp = requests.get(url, headers=HEADERS, params=params)
    return resp.json().get("response", [])

def buscar_stats(team_id, league_id="71"):
    url = "https://api-football-v1.p.rapidapi.com/v3/teams/statistics"
    params = {"team": team_id, "league": league_id, "season": "2025"}
    resp = requests.get(url, headers=HEADERS, params=params)
    data = resp.json().get("response", {})
    shots = data.get("shots", {})
    jogos = data.get("fixtures", {}).get("played", {}).get("total", 1) or 1
    total = shots.get("total", {}).get("total", 0) or 0
    no_alvo = shots.get("on", {}).get("total", 0) or 0
    gols = data.get("goals", {}).get("for", {}).get("total", {}).get("total", 0) or 0
    media = round(total / jogos, 1)
    media_alvo = round(no_alvo / jogos, 1)
    ofensividade = min(round((media / 20) * 100), 100)
    return {
        "media": media,
        "media_alvo": media_alvo,
        "ofensividade": ofensividade,
        "jogos": jogos,
        "gols": gols,
    }

def buscar_escalacao(fixture_id):
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures/lineups"
    params = {"fixture": fixture_id}
    resp = requests.get(url, headers=HEADERS, params=params)
    return resp.json().get("response", [])

def calcular_confianca(total_media, importancia):
    base = min(total_media / 30 * 70, 70)
    bonus = importancia * 3
    return min(round(base + bonus), 99)

def calcular_importancia(fixture):
    status = fixture.get("league", {}).get("round", "")
    if "Final" in status or "mata" in status.lower():
        return 10
    if "Semi" in status or "Quartas" in status:
        return 9
    if "Oitavas" in status or "Copa" in status:
        return 8
    return 7

def formatar_alerta(fixture, stats_casa, stats_fora, dias_restantes=0):
    casa = fixture["teams"]["home"]["name"]
    fora = fixture["teams"]["away"]["name"]
    hora = fixture["fixture"]["date"][11:16]
    data = fixture["fixture"]["date"][:10]
    liga = fixture["league"]["name"]
    total_media = stats_casa["media"] + stats_fora["media"]
    importancia = calcular_importancia(fixture)
    confianca = calcular_confianca(total_media, importancia)

    if dias_restantes == 0:
        prefixo = "🚨 *ALERTA HOJE*"
    elif dias_restantes == 1:
        prefixo = "⚡ *ALERTA — AMANHÃ*"
    elif dias_restantes == 2:
        prefixo = "📅 *ANTECIPADO — 2 DIAS*"
    else:
        prefixo = "📅 *ANTECIPADO — 3 DIAS*"

    mercado_linha = round(total_media - 2.5, 1)

    msg = (
        f"{prefixo}\n\n"
        f"🏆 {liga}\n"
        f"⚽ *{casa} x {fora}*\n"
        f"🕐 {data} às {hora}\n\n"
        f"📊 *Análise de Finalizações:*\n"
        f"🏠 {casa}: {stats_casa['media']} fin/jogo | {stats_casa['media_alvo']} no alvo\n"
        f"✈️ {fora}: {stats_fora['media']} fin/jogo | {stats_fora['media_alvo']} no alvo\n"
        f"📈 Média combinada: *{total_media} fin/jogo*\n\n"
        f"🎯 *Ofensividade:*\n"
        f"🏠 {casa}: {stats_casa['ofensividade']}%\n"
        f"✈️ {fora}: {stats_fora['ofensividade']}%\n\n"
        f"🔥 *Importância do jogo:* {importancia}/10\n"
        f"✅ *Confiança:* {confianca}%\n\n"
        f"💰 *Mercado sugerido:*\n"
        f"Ambos finalizações Over {mercado_linha}\n\n"
        f"🏦 *Melhores casas agora:*\n"
        f"• Superbet — verificar odd\n"
        f"• Bet365 — verificar odd\n"
        f"• Betano — verificar odd\n"
    )

    if dias_restantes > 0:
        msg += (
            f"\n⏰ *Dica de antecipação:*\n"
            f"Entrar agora pode garantir odd melhor!\n"
            f"Odds tendem a cair conforme o jogo se aproxima."
        )

    return msg, confianca

def analisar_jogos(fixtures, dias_restantes=0, limite=20):
    alertas = []
    for f in fixtures[:limite]:
        id_casa = f["teams"]["home"]["id"]
        id_fora = f["teams"]["away"]["id"]
        league_id = str(f["league"]["id"])
        stats_casa = buscar_stats(id_casa, league_id)
        stats_fora = buscar_stats(id_fora, league_id)
        total = stats_casa["media"] + stats_fora["media"]
        if total >= 18:
            msg, confianca = formatar_alerta(f, stats_casa, stats_fora, dias_restantes)
            alertas.append((confianca, msg))
    alertas.sort(reverse=True)
    return alertas

# ─── ALERTA AUTOMÁTICO DIÁRIO ─────────────────────────────────────────────────

def alerta_automatico(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    hoje = date.today()
    bot.send_message(chat_id=CHAT_ID, text="🌅 *Bom dia! Analisando jogos de hoje e próximos dias...*", parse_mode="Markdown")

    for dias in range(4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, league_id in LIGAS.items():
            fixtures = buscar_jogos_data(data_str, league_id)
            if not fixtures:
                continue
            alertas = analisar_jogos(fixtures, dias_restantes=dias)
            for confianca, msg in alertas:
                if confianca >= 70:
                    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ─── COMANDOS ─────────────────────────────────────────────────────────────────

def start(update, context):
    update.message.reply_text(
        "🤖 *Finalizações Bot online!*\n\n"
        "Comandos:\n"
        "/jogos — Jogos do Brasileirão hoje\n"
        "/alerta — Alertas de finalizações hoje\n"
        "/antecipados — Jogos dos próximos 3 dias\n"
        "/setid — Ativar alertas automáticos\n"
        "/ping — Testar bot",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot online e funcionando!")

def setid(update, context):
    chat_id = str(update.message.chat_id)
    update.message.reply_text(
        f"✅ Seu Chat ID é: `{chat_id}`\n\n"
        f"Adicione no Railway em Variables:\n"
        f"Key: `CHAT_ID`\n"
        f"Value: `{chat_id}`\n\n"
        f"Depois os alertas automáticos chegam todo dia às 08h!",
        parse_mode="Markdown"
    )

def jogos(update, context):
    update.message.reply_text("🔍 Buscando jogos de hoje...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, league_id in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, league_id)
        if not fixtures:
            continue
        encontrou = True
        msg = f"⚽ *{nome_liga} — Hoje:*\n\n"
        for f in fixtures[:5]:
            casa = f["teams"]["home"]["name"]
            fora = f["teams"]["away"]["name"]
            hora = f["fixture"]["date"][11:16]
            msg += f"🕐 {hora} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhum jogo encontrado hoje nas ligas monitoradas.")

def alerta(update, context):
    update.message.reply_text("📊 Analisando finalizações de hoje...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, league_id in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, league_id)
        if not fixtures:
            continue
        alertas = analisar_jogos(fixtures, dias_restantes=0)
        for confianca, msg in alertas:
            encontrou = True
            update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("⚠️ Nenhum jogo com média alta hoje.")

def antecipados(update, context):
    update.message.reply_text("📅 Buscando oportunidades nos próximos 3 dias...")
    hoje = date.today()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, league_id in LIGAS.items():
            fixtures = buscar_jogos_data(data_str, league_id)
            if not fixtures:
                continue
            alertas = analisar_jogos(fixtures, dias_restantes=dias)
            for confianca, msg in alertas:
                encontrou = True
                update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhuma oportunidade antecipada encontrada.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("jogos", jogos))
    dp.add_handler(CommandHandler("alerta", alerta))
    dp.add_handler(CommandHandler("antecipados", antecipados))
    dp.add_handler(CommandHandler("setid", setid))

    # Alerta automático todo dia às 08h (horário UTC-3 = 11h UTC)
    updater.job_queue.run_daily(
        alerta_automatico,
        time=__import__("datetime").time(hour=11, minute=0)
    )

    print("Bot rodando...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
