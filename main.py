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
FD_KEY = os.environ.get("API_KEY") # Football-Data.org
SPORTMONKS_KEY = os.environ.get("SPORTMONKS_KEY") # Sua key

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── APIS ─────────────────────────────────────────────────────────────────────
FD_HEADERS = {"X-Auth-Token": FD_KEY}
FD_BASE = "https://api.football-data.org/v4"
fd_session = requests.Session()
fd_session.headers.update(FD_HEADERS)

SPORTMONKS_BASE = "https://api.sportmonks.com/v3/football"
sport_session = requests.Session()

LIGAS = {
    "Brasileirao": {"fd_id": "2013", "sm_id": 636},
    "Premier League": {"fd_id": "2021", "sm_id": 8},
    "La Liga": {"fd_id": "2014", "sm_id": 564},
    "Serie A Italia": {"fd_id": "2019", "sm_id": 384},
    "Bundesliga": {"fd_id": "2002", "sm_id": 82},
    "Ligue 1": {"fd_id": "2015", "sm_id": 301},
    "Champions League": {"fd_id": "2001", "sm_id": 2},
    "Europa League": {"fd_id": "2146", "sm_id": 5},
    "Libertadores": {"fd_id": "2152", "sm_id": 876},
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
    if item and time.time() - item["time"] < 21600: # 6h
        return item["data"]
    return None

def cache_set(key, value):
    _cache[key] = {"data": value, "time": time.time()}
    salvar_cache(_cache)

# ─── FOOTBALL-DATA.ORG ────────────────────────────────────────────────────────
def fd_request(endpoint):
    url = f"{FD_BASE}/{endpoint}"
    try:
        resp = fd_session.get(url, timeout=15)
        if resp.status_code == 429:
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

# ─── SPORTMONKS ───────────────────────────────────────────────────────────────
def sm_request(endpoint):
    url = f"{SPORTMONKS_BASE}/{endpoint}"
    params = {"api_token": SPORTMONKS_KEY}
    try:
        resp = sport_session.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            logging.warning("Rate limit Sportmonks. Aguardando 60s")
            time.sleep(60)
            return sm_request(endpoint)
        return resp.json()
    except Exception as e:
        logging.error(f"Exception SM {endpoint}: {e}")
        return {}

def buscar_time_sm_id(nome_time):
    key = f"sm_team_{nome_time}"
    cached = cache_get(key)
    if cached: return cached

    # Remove FC, CF, etc pra melhorar busca
    nome_busca = nome_time.replace(" FC", "").replace(" CF", "").replace(" EC", "")
    data = sm_request(f"teams/search/{nome_busca}")

    if data and data.get("data"):
        team_id = data["data"][0]["id"]
        logging.info(f"Sportmonks MATCH: {nome_time} -> {team_id}")
        cache_set(key, team_id)
        return team_id
    return None

def buscar_media_stats_sm(nome_time):
    key = f"sm_stats_{nome_time}"
    cached = cache_get(key)
    if cached: return cached

    team_id = buscar_time_sm_id(nome_time)
    if not team_id:
        return {"media_shots": 0, "media_sot": 0, "media_corners": 0, "media_goals": 0, "jogos": 0}

    # Pega últimos 5 jogos do time
    data = sm_request(f"teams/{team_id}/latest?include=statistics")
    if not data or not data.get("data"):
        return {"media_shots": 0, "media_sot": 0, "media_corners": 0, "media_goals": 0, "jogos": 0}

    fixtures = data["data"][:5]
    total_shots = total_sot = total_corners = total_goals = 0
    jogos_validos = 0

    for fix in fixtures:
        stats = fix.get("statistics", [])
        is_home = fix["participants"][0]["id"] == team_id

        for stat in stats:
            if stat["type_id"] == 34: # Total Shots
                total_shots += stat["data"]["value"] if is_home else stat["data"]["value"]
            if stat["type_id"] == 86: # Shots on Target
                total_sot += stat["data"]["value"] if is_home else stat["data"]["value"]
            if stat["type_id"] == 35: # Corners
                total_corners += stat["data"]["value"] if is_home else stat["data"]["value"]

        # Gols
        if is_home:
            total_goals += fix["scores"][0]["score"]
        else:
            total_goals += fix["scores"][1]["score"]

        jogos_validos += 1
        time.sleep(1)

    if jogos_validos > 0:
        resultado = {
            "media_shots": round(total_shots / jogos_validos, 1),
            "media_sot": round(total_sot / jogos_validos, 1),
            "media_corners": round(total_corners / jogos_validos, 1),
            "media_goals": round(total_goals / jogos_validos, 1),
            "jogos": jogos_validos
        }
    else:
        resultado = {"media_shots": 0, "media_sot": 0, "media_corners": 0, "media_goals": 0, "jogos": 0}

    cache_set(key, resultado)
    logging.info(f"SM Stats {nome_time}: {resultado}")
    return resultado

def analisar_confronto(match, nome_liga):
    casa = match["homeTeam"]["name"]
    fora = match["awayTeam"]["name"]

    stats_casa = buscar_media_stats_sm(casa)
    stats_fora = buscar_media_stats_sm(fora)

    return {
        "media_shots": round(stats_casa["media_shots"] + stats_fora["media_shots"], 1),
        "media_sot": round(stats_casa["media_sot"] + stats_fora["media_sot"], 1),
        "media_corners": round(stats_casa["media_corners"] + stats_fora["media_corners"], 1),
        "media_goals": round(stats_casa["media_goals"] + stats_fora["media_goals"], 1),
        "casa_shots": stats_casa["media_shots"],
        "fora_shots": stats_fora["media_shots"],
        "casa_corners": stats_casa["media_corners"],
        "fora_corners": stats_fora["media_corners"],
        "jogos_casa": stats_casa["jogos"],
        "jogos_fora": stats_fora["jogos"]
    }

# ─── FORMATADORES ─────────────────────────────────────────────────────────────
def formatar_alerta_desregulado(match, nome_liga):
    casa = match["homeTeam"]["name"]
    fora = match["awayTeam"]["name"]
    liga = match["competition"]["name"]

    hora_utc_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    hora_brt = hora_utc_dt - timedelta(hours=3)
    hora_brt_str = hora_brt.strftime("%H:%M")
    data_str = hora_brt.strftime("%d/%m")

    stats = analisar_confronto(match, nome_liga)
    media_shots = stats["media_shots"]
    media_corners = stats["media_corners"]
    media_goals = stats["media_goals"]

    # Lógica de entrada desregulada
    alertas = []

    # Over Chutes: projeção 20+ mas linha deve estar 8.5 ou 9.5
    if media_shots >= 22:
        linha_justa = round(media_shots * 0.45, 1) # 45% dos chutes = linha justa
        alertas.append(f"🎯 *Over Chutes*\n Projeção: {media_shots} | Linha justa: {linha_justa}\n Buscar: Over {linha_justa - 1} @1.85+")

    # Over Escanteios
    if media_corners >= 10:
        linha_justa = round(media_corners * 0.85, 1)
        alertas.append(f"🚩 *Over Escanteios*\n Projeção: {media_corners} | Linha justa: {linha_justa}\n Buscar: Over {linha_justa - 0.5} @1.90+")

    # Over Gols
    if media_goals >= 3.0:
        alertas.append(f"⚽ *Over Gols*\n Projeção: {media_goals} | Buscar: Over 2.5 @1.80+")

    if not alertas:
        return None

    nivel = "🚨🔥 ENTRADA DESREGULADA" if media_shots >= 25 else "🚨 JOGO QUENTE"

    msg = (
        f"{nivel}\n{'━' * 28}\n🏆 {liga}\n⚽ *{casa} x {fora}*\n"
        f"🕐 {data_str} às {hora_brt_str} BRT\n\n"
        f"📊 *Média Últimos 5 Jogos:*\n"
        f"• Chutes: {media_shots} ({stats['casa_shots']}/{stats['fora_shots']})\n"
        f"• No alvo: {stats['media_sot']}\n"
        f"• Escanteios: {media_corners}\n"
        f"• Gols: {media_goals}\n\n"
        + "\n\n".join(alertas)
    )
    return msg

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
def start(update, context):
    update.message.reply_text(
        "🤖 *Shot Alert Bot v6.8 - Sportmonks*\n\n"
        "🌍 9 ligas • 📊 Chutes, Escanteios, Gols\n"
        "🎯 Detecta linhas desreguladas\n\n"
        "/ping — Testar\n/jogos — Hoje\n/antecipados — 3 dias\n/alerta — Entradas quentes\n/diagnostico — API",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v6.8 online! Sportmonks ativo")

def diagnostico(update, context):
    update.message.reply_text("🔬 Testando Football-Data...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    resultado = ""
    total = 0
    for nome, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["fd_id"])
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
        matches = buscar_jogos_data(hoje, info["fd_id"])
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
    update.message.reply_text("📅 Analisando próximos 3 dias... Pode demorar 1min")
    hoje = datetime.now(timezone.utc).date()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            matches = buscar_jogos_data(data_str, info["fd_id"])
            for m in matches[:3]:
                msg = formatar_alerta_desregulado(m, nome_liga)
                if msg:
                    encontrou = True
                    update.message.reply_text(msg, parse_mode="Markdown")
                    time.sleep(4)
    if not encontrou:
        update.message.reply_text("😴 Nenhuma entrada desregulada nos próximos 3 dias.")

def alerta(update, context):
    update.message.reply_text("🔥 Buscando entradas quentes hoje...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    quente = False
    for nome_liga, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["fd_id"])
        for m in matches:
            msg = formatar_alerta_desregulado(m, nome_liga)
            if msg:
                quente = True
                update.message.reply_text(msg, parse_mode="Markdown")
                time.sleep(4)
    if not quente:
        update.message.reply_text("😴 Nenhuma entrada desregulada hoje.")

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
    return 'Bot v6.8 Sportmonks Online'

@app.route('/health')
def health():
    return 'ok', 200

if __name__ == "__main__":
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        bot.set_webhook(url=f"{RAILWAY_URL}/{TOKEN}")
        logging.info("Webhook setado")
    PORT = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=PORT)
