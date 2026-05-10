import os
import json
import logging
import requests
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler

TOKEN = os.environ.get("BOT_TOKEN")
API_KEY = os.environ.get("API_KEY") # Token do Football-Data.org

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── APIS ─────────────────────────────────────────────────────────────────────
FD_HEADERS = {"X-Auth-Token": API_KEY}
FD_BASE = "https://api.football-data.org/v4"
session = requests.Session()
session.headers.update(FD_HEADERS)

SOFASCORE_BASE = "https://api.sofascore.com/api/v1"
SOFASCORE_SESSION = requests.Session()
SOFASCORE_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

LIGAS = {
    "Brasileirao": {"id": "2013", "code": "BSA"},
    "Premier League": {"id": "2021", "code": "PL"},
    "La Liga": {"id": "2014", "code": "PD"},
    "Serie A Italia": {"id": "2019", "code": "SA"},
    "Bundesliga": {"id": "2002", "code": "BL1"},
    "Ligue 1": {"id": "2015", "code": "FL1"},
    "Champions League": {"id": "2001", "code": "CL"},
    "Europa League": {"id": "2146", "code": "EL"},
    "Libertadores": {"id": "2152", "code": "CLI"},
}

# ─── CACHE ────────────────────────────────────────────────────────────────────
CACHE_FILE = "cache.json"

def carregar_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def salvar_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)

_cache = carregar_cache()

def cache_get(key):
    item = _cache.get(key)
    if item and time.time() - item["time"] < 3600:
        return item["data"]
    return None

def cache_set(key, value):
    _cache[key] = {"data": value, "time": time.time()}
    salvar_cache(_cache)

# ─── FOOTBALL-DATA.ORG ────────────────────────────────────────────────────────
def fd_request(endpoint):
    url = f"{FD_BASE}/{endpoint}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 429:
            logging.warning("Rate limit FD. Aguardando 60s")
            time.sleep(60)
            return fd_request(endpoint)
        data = resp.json()
        if data.get("errorCode"):
            logging.error(f"FD ERR {endpoint}: {data.get('message')}")
        return data
    except Exception as e:
        logging.error(f"Exception FD {endpoint}: {e}")
        return {}

def buscar_jogos_data(data_str, comp_id):
    key = f"jogos_{data_str}_{comp_id}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    endpoint = f"competitions/{comp_id}/matches?dateFrom={data_str}&dateTo={data_str}"
    data = fd_request(endpoint)
    result = data.get("matches", [])
    logging.info(f"FD {comp_id} {data_str}: {len(result)} jogos")
    cache_set(key, result)
    time.sleep(6) # 10 req/min free
    return result

# ─── SOFASCORE STATS ──────────────────────────────────────────────────────────
def buscar_id_sofascore(time_casa, time_fora, data_str):
    key = f"sofa_id_{time_casa}_{time_fora}_{data_str}"
    cached = cache_get(key)
    if cached: return cached

    try:
        url = f"{SOFASCORE_BASE}/sport/football/scheduled-events/{data_str}"
        resp = SOFASCORE_SESSION.get(url, timeout=10)
        eventos = resp.json().get("events", [])
        for e in eventos:
            if (time_casa.lower() in e["homeTeam"]["name"].lower() or
                e["homeTeam"]["name"].lower() in time_casa.lower()) and \
               (time_fora.lower() in e["awayTeam"]["name"].lower() or
                e["awayTeam"]["name"].lower() in time_fora.lower()):
                cache_set(key, e["id"])
                return e["id"]
    except Exception as e:
        logging.error(f"Sofascore ID ERR: {e}")
    return None

def buscar_stats_sofascore(match_fd):
    casa = match_fd["homeTeam"]["name"]
    fora = match_fd["awayTeam"]["name"]
    data_str = match_fd["utcDate"][:10]

    key = f"stats_{casa}_{fora}_{data_str}"
    cached = cache_get(key)
    if cached: return cached

    match_id = buscar_id_sofascore(casa, fora, data_str)
    if not match_id:
        return {"media": 0, "media_alvo": 0, "jogos": 0}

    try:
        url = f"{SOFASCORE_BASE}/event/{match_id}/statistics"
        resp = SOFASCORE_SESSION.get(url, timeout=10)
        stats = resp.json()

        shots_casa = shots_fora = 0
        sot_casa = sot_fora = 0

        for group in stats.get("statistics", []):
            if group["period"] == "ALL":
                for stat_group in group.get("groups", []):
                    for stat in stat_group.get("statisticsItems", []):
                        if stat["name"] == "Total shots":
                            shots_casa = int(stat["home"])
                            shots_fora = int(stat["away"])
                        if stat["name"] == "Shots on target":
                            sot_casa = int(stat["home"])
                            sot_fora = int(stat["away"])

        resultado = {
            "media": shots_casa + shots_fora,
            "media_alvo": sot_casa + sot_fora,
            "jogos": 1
        }
        cache_set(key, resultado)
        return resultado
    except Exception as e:
        logging.error(f"Sofascore Stats ERR: {e}")
        return {"media": 0, "media_alvo": 0, "jogos": 0}

# ─── ANÁLISE ──────────────────────────────────────────────────────────────────
def formatar_alerta(match):
    casa = match["homeTeam"]["name"]
    fora = match["awayTeam"]["name"]
    liga = match["competition"]["name"]

    hora_utc_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    hora_brt = hora_utc_dt - timedelta(hours=3)
    hora_brt_str = hora_brt.strftime("%H:%M")
    data_str = hora_brt.strftime("%d/%m")

    stats = buscar_stats_sofascore(match)
    media_shots = stats["media"]
    media_sot = stats["media_alvo"]

    if media_shots >= 25:
        nivel = "🚨🔥 JOGO EXPLOSIVO"
    elif media_shots >= 18:
        nivel = "🚨 JOGO QUENTE"
    elif media_shots >= 12:
        nivel = "⚠️ Jogo ok"
    else:
        nivel = "📊 Jogo monitorado"

    msg = (
        f"{nivel}\n{'━' * 28}\n🏆 {liga}\n⚽ *{casa} x {fora}*\n"
        f"🕐 {data_str} às {hora_brt_str} BRT\n\n"
        f"📊 *Últimos jogos:*\n"
        f"• Finalizações: {media_shots if media_shots else 'N/A'}\n"
        f"• No alvo: {media_sot if media_sot else 'N/A'}\n\n"
        f"💰 *Mercado: Over Finalizações/Gols*"
    )
    return msg

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
def start(update, context):
    update.message.reply_text(
        "🤖 *Shot Alert Bot v6.6*\n\n"
        "🌍 9 ligas • 📊 Football-Data + Sofascore\n\n"
        "/ping — Testar\n/jogos — Hoje\n/antecipados — 3 dias\n/alerta — Buscar quente\n/diagnostico — API",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v6.6 online! FD + Sofascore")

def diagnostico(update, context):
    update.message.reply_text("🔬 Testando APIs...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    resultado = ""
    total = 0
    for nome, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["id"])
        count = len(matches)
        if count > 0:
            resultado += f"✅ {nome}: {count}\n"
            total += count
        else:
            resultado += f"⚪ {nome}: 0\n"
    update.message.reply_text(f"📊 *Diagnóstico — {hoje}*\n\n{resultado}\n*Total: {total} jogos*", parse_mode="Markdown")

def jogos(update, context):
    update.message.reply_text("🔍 Buscando jogos de hoje...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["id"])
        if not matches: continue
        encontrou = True
        msg = f"⚽ *{nome_liga}:*\n"
        for m in matches[:8]:
            casa = m["homeTeam"]["name"]
            fora = m["awayTeam"]["name"]
            hora_utc_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
            hora_brt = hora_utc_dt - timedelta(hours=3)
            hora_brt_str = hora_brt.strftime("%H:%M")
            msg += f"🕐 {hora_brt_str} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhum jogo hoje.")

def antecipados(update, context):
    update.message.reply_text("📅 Buscando próximos 3 dias...")
    hoje = datetime.now(timezone.utc).date()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            matches = buscar_jogos_data(data_str, info["id"])
            for m in matches:
                encontrou = True
                msg = formatar_alerta(m)
                update.message.reply_text(msg, parse_mode="Markdown")
                time.sleep(2) # Evita spam no Telegram
    if not encontrou:
        update.message.reply_text("📭 Nenhuma partida nos próximos 3 dias.")

def alerta(update, context):
    update.message.reply_text("🔥 Buscando jogos quentes hoje...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    quente = False
    for nome_liga, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["id"])
        for m in matches:
            stats = buscar_stats_sofascore(m)
            if stats["media"] >= 18: # Filtro: 18+ finalizações
                quente = True
                msg = formatar_alerta(m)
                update.message.reply_text(msg, parse_mode="Markdown")
                time.sleep(2)
    if not quente:
        update.message.reply_text("😴 Nenhum jogo quente hoje. Usa /jogos pra ver todos.")

# ─── FLASK + WEBHOOK ──────────────────────────────────────────────────────────
app = Flask(__name__)
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, None, workers=1, use_context=True)

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("ping", ping))
dp.add_handler(CommandHandler("jogos", jogos))
dp.add_handler(CommandHandler("antecipados", antecipados))
dp.add_handler(CommandHandler("alerta", alerta))
dp.add_handler(CommandHandler("diagnostico", diagnostico))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return 'ok'

@app.route('/')
def index():
    return 'Bot v6.6 FD+Sofascore Online'

@app.route('/health')
def health():
    return 'ok', 200

@app.route('/setwebhook')
def setwebhook():
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    webhook_url = f"{RAILWAY_URL}/{TOKEN}"
    bot.set_webhook(url=webhook_url)
    return f'Webhook setado: {RAILWAY_URL}'

if __name__ == "__main__":
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        bot.set_webhook(url=f"{RAILWAY_URL}/{TOKEN}")
        logging.info("Webhook setado")
    PORT = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=PORT)
