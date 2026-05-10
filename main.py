import os
import json
import logging
import requests
import time
import re
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
    if item and time.time() - item["time"] < 21600: # 6h cache pra stats
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
        return resp.json()
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
    time.sleep(6)
    return result

# ─── SOFASCORE STATS ──────────────────────────────────────────────────────────
def limpar_nome_time(nome):
    nome = nome.lower()
    nome = re.sub(r'\b(fc|ec|sc|ac|cf|if|fk|bk|sk|bk|afc|ca|cr|se)\b', '', nome)
    nome = re.sub(r'[^a-z0-9 ]', '', nome)
    return nome.strip()

def buscar_time_sofascore(nome_time):
    key = f"sofa_team_{nome_time}"
    cached = cache_get(key)
    if cached: return cached

    try:
        url = f"{SOFASCORE_BASE}/search/teams/{nome_time}"
        resp = SOFASCORE_SESSION.get(url, timeout=10)
        results = resp.json().get("results", [])
        if results:
            team_id = results[0]["entity"]["id"]
            cache_set(key, team_id)
            return team_id
    except Exception as e:
        logging.error(f"Sofascore search team ERR {nome_time}: {e}")
    return None

def buscar_media_stats_time(nome_time):
    key = f"stats_avg_{nome_time}"
    cached = cache_get(key)
    if cached: return cached

    team_id = buscar_time_sofascore(nome_time)
    if not team_id:
        return {"media_shots": 0, "media_sot": 0, "jogos": 0}

    try:
        url = f"{SOFASCORE_BASE}/team/{team_id}/events/last/0"
        resp = SOFASCORE_SESSION.get(url, timeout=10)
        events = resp.json().get("events", [])[:5] # Últimos 5 jogos

        total_shots = total_sot = jogos_validos = 0

        for event in events:
            if event["status"]["type"]!= "finished": continue
            event_id = event["id"]

            url_stats = f"{SOFASCORE_BASE}/event/{event_id}/statistics"
            resp_stats = SOFASCORE_SESSION.get(url_stats, timeout=10)
            stats = resp_stats.json()

            is_home = event["homeTeam"]["id"] == team_id

            for group in stats.get("statistics", []):
                if group["period"] == "ALL":
                    for stat_group in group.get("groups", []):
                        for stat in stat_group.get("statisticsItems", []):
                            if stat["name"] == "Total shots":
                                shots = int(stat["home"] if is_home else stat["away"] or 0)
                                total_shots += shots
                            if stat["name"] == "Shots on target":
                                sot = int(stat["home"] if is_home else stat["away"] or 0)
                                total_sot += sot
            jogos_validos += 1
            time.sleep(1) # Evita ban

        if jogos_validos > 0:
            resultado = {
                "media_shots": round(total_shots / jogos_validos, 1),
                "media_sot": round(total_sot / jogos_validos, 1),
                "jogos": jogos_validos
            }
        else:
            resultado = {"media_shots": 0, "media_sot": 0, "jogos": 0}

        cache_set(key, resultado)
        logging.info(f"Stats {nome_time}: {resultado}")
        return resultado
    except Exception as e:
        logging.error(f"Sofascore stats avg ERR {nome_time}: {e}")
        return {"media_shots": 0, "media_sot": 0, "jogos": 0}

def analisar_confronto(match):
    casa = match["homeTeam"]["name"]
    fora = match["awayTeam"]["name"]

    stats_casa = buscar_media_stats_time(casa)
    stats_fora = buscar_media_stats_time(fora)

    media_combinada = stats_casa["media_shots"] + stats_fora["media_shots"]
    media_sot_combinada = stats_casa["media_sot"] + stats_fora["media_sot"]

    return {
        "media_shots": round(media_combinada, 1),
        "media_sot": round(media_sot_combinada, 1),
        "casa_shots": stats_casa["media_shots"],
        "fora_shots": stats_fora["media_shots"],
        "jogos_casa": stats_casa["jogos"],
        "jogos_fora": stats_fora["jogos"]
    }

# ─── FORMATADORES ─────────────────────────────────────────────────────────────
def formatar_alerta(match):
    casa = match["homeTeam"]["name"]
    fora = match["awayTeam"]["name"]
    liga = match["competition"]["name"]

    hora_utc_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    hora_brt = hora_utc_dt - timedelta(hours=3)
    hora_brt_str = hora_brt.strftime("%H:%M")
    data_str = hora_brt.strftime("%d/%m")

    stats = analisar_confronto(match)
    media_shots = stats["media_shots"]

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
        f"📊 *Média Últimos 5 Jogos:*\n"
        f"• Combinada: {media_shots} finalizações\n"
        f"• No alvo: {stats['media_sot']}\n"
        f"• {casa}: {stats['casa_shots']} ({stats['jogos_casa']}j)\n"
        f"• {fora}: {stats['fora_shots']} ({stats['jogos_fora']}j)\n\n"
        f"💰 *Mercado: Over Finalizações*"
    )
    return msg

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
def start(update, context):
    update.message.reply_text(
        "🤖 *Shot Alert Bot v6.7*\n\n"
        "🌍 9 ligas • 📊 Média últimos 5 jogos\n\n"
        "/ping — Testar\n/jogos — Hoje\n/antecipados — 3 dias\n/alerta — Jogos quentes\n/diagnostico — API",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v6.7 online! Média Sofascore ativa")

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
    update.message.reply_text("📅 Analisando próximos 3 dias... Demora 30s por jogo")
    hoje = datetime.now(timezone.utc).date()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            matches = buscar_jogos_data(data_str, info["id"])
            for m in matches[:3]: # Limita 3 por liga pra não demorar
                encontrou = True
                msg = formatar_alerta(m)
                update.message.reply_text(msg, parse_mode="Markdown")
                time.sleep(3)
    if not encontrou:
        update.message.reply_text("📭 Nenhuma partida nos próximos 3 dias.")

def alerta(update, context):
    update.message.reply_text("🔥 Buscando jogos quentes... Pode demorar 1min")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    quente = False
    for nome_liga, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["id"])
        for m in matches:
            stats = analisar_confronto(m)
            if stats["media_shots"] >= 18:
                quente = True
                msg = formatar_alerta(m)
                update.message.reply_text(msg, parse_mode="Markdown")
                time.sleep(3)
    if not quente:
        update.message.reply_text("😴 Nenhum jogo com 18+ finalizações hoje.")

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
    return 'Bot v6.7 FD+Sofascore Online'

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
