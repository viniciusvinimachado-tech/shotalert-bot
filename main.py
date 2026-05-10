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
FD_KEY = os.environ.get("API_KEY")
SPORTMONKS_KEY = os.environ.get("SPORTMONKS_KEY")

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

MAPEAMENTO_SM = {
    "Tottenham Hotspur FC": 6, "Leeds United FC": 63, "Manchester City FC": 9,
    "Crystal Palace FC": 7, "Manchester United FC": 14, "Newcastle United FC": 20,
    "West Ham United FC": 1, "Brighton & Hove Albion FC": 30, "Wolverhampton Wanderers FC": 3,
    "Nottingham Forest FC": 64, "Aston Villa FC": 40, "Chelsea FC": 18, "Arsenal FC": 19,
    "Liverpool FC": 26, "Everton FC": 45, "Fulham FC": 43, "Brentford FC": 59,
    "AFC Bournemouth": 35, "Luton Town FC": 58, "Sheffield United FC": 54, "Burnley FC": 50,
    "Rayo Vallecano de Madrid": 89, "Girona FC": 164, "RC Celta de Vigo": 85,
    "Levante UD": 93, "Real Betis Balompié": 83, "Elche CF": 95, "CA Osasuna": 88,
    "Club Atlético de Madrid": 82, "RCD Espanyol de Barcelona": 80, "Athletic Club": 79,
    "Villarreal CF": 94, "Sevilla FC": 84, "Deportivo Alavés": 81, "FC Barcelona": 86,
    "Real Madrid CF": 78, "Real Sociedad de Fútbol": 92, "Valencia CF": 76, "Getafe CF": 77,
    "Real Valladolid CF": 98, "UD Almería": 100, "RCD Mallorca": 87,
    "SSC Napoli": 113, "Bologna FC 1909": 104, "Inter Milan": 108, "AC Milan": 102,
    "AS Roma": 106, "Juventus FC": 109, "Atalanta BC": 117, "ACF Fiorentina": 112,
    "SS Lazio": 110, "Torino FC": 115, "Udinese Calcio": 103, "US Sassuolo Calcio": 116,
    "US Lecce": 120, "Genoa CFC": 107, "Empoli FC": 121, "Frosinone Calcio": 124,
    "Hellas Verona FC": 118, "US Salernitana 1919": 123, "Cagliari Calcio": 105,
    "FC Bayern München": 157, "Bayer 04 Leverkusen": 162, "Borussia Mönchengladbach": 164,
    "RB Leipzig": 174, "Borussia Dortmund": 165, "Eintracht Frankfurt": 167,
    "VfL Wolfsburg": 158, "TSG 1899 Hoffenheim": 168, "VfB Stuttgart": 159,
    "1. FC Union Berlin": 170, "1. FC Köln": 169, "SC Freiburg": 161, "1. FSV Mainz 05": 160,
    "FC Augsburg": 172, "SV Werder Bremen": 166, "VfL Bochum 1848": 171,
    "1. FC Heidenheim 1846": 175, "SV Darmstadt 98": 173,
    "Paris Saint-Germain FC": 85, "AS Monaco FC": 79, "Olympique de Marseille": 81,
    "Olympique Lyonnais": 80, "OGC Nice": 82, "Stade Rennais FC": 78, "RC Lens": 74,
    "LOSC Lille": 75, "Stade Brestois 29": 77, "RC Strasbourg Alsace": 76,
    "Montpellier Hérault SC": 72, "Toulouse FC": 73, "FC Nantes": 70, "Stade de Reims": 71,
    "Clermont Foot 63": 69, "Le Havre AC": 68, "FC Metz": 67, "FC Lorient": 66,
    "CR Flamengo": 204, "SE Palmeiras": 202, "SC Corinthians Paulista": 205,
    "São Paulo FC": 203, "Grêmio FBPA": 207, "SC Internacional": 208, "Cruzeiro EC": 206,
    "Fluminense FC": 201, "Botafogo FR": 209, "CA Paranaense": 210, "EC Bahia": 211,
    "Fortaleza EC": 212, "Red Bull Bragantino": 213, "Cuiabá EC": 214, "CR Vasco da Gama": 215,
    "Atlético Mineiro": 216, "Coritiba FC": 217, "América FC": 218, "Goiás EC": 219, "Santos FC": 220,
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
    if item and time.time() - item["time"] < 21600:
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

# ─── SPORTMONKS - PLANO GRÁTIS ────────────────────────────────────────────────
def sm_request(endpoint, params=None):
    url = f"{SPORTMONKS_BASE}/{endpoint}"
    base_params = {"api_token": SPORTMONKS_KEY}
    if params:
        base_params.update(params)
    try:
        resp = sport_session.get(url, params=base_params, timeout=15)
        if resp.status_code == 429:
            logging.warning("Rate limit Sportmonks. Aguardando 60s")
            time.sleep(60)
            return sm_request(endpoint, params)
        if resp.status_code!= 200:
            logging.warning(f"SM status {resp.status_code}: {endpoint}")
            return None
        return resp.json()
    except Exception as e:
        logging.error(f"Exception SM {endpoint}: {e}")
        return None

def buscar_media_stats_sm(nome_time):
    key = f"sm_stats_{nome_time}"
    cached = cache_get(key)
    if cached: return cached

    team_id = MAPEAMENTO_SM.get(nome_time)
    if not team_id:
        logging.warning(f"SM: ID não mapeado {nome_time}")
        return {"media_shots": 0, "media_sot": 0, "media_corners": 0, "media_goals": 0, "jogos": 0}

    # Endpoint que funciona no grátis: últimos 5 jogos finalizados do time
    params = {
        "filter[team_id]": team_id,
        "filter[status]": "FT",
        "include": "statistics;scores;participants",
        "sort": "-starting_at",
        "per_page": 5
    }
    data = sm_request("fixtures", params)

    if not data or not data.get("data"):
        logging.warning(f"SM: Sem dados {nome_time}")
        return {"media_shots": 0, "media_sot": 0, "media_corners": 0, "media_goals": 0, "jogos": 0}

    fixtures = data["data"]
    total_shots = total_sot = total_corners = total_goals = 0
    jogos_validos = 0

    for fix in fixtures:
        participants = fix.get("participants", [])
        if len(participants) < 2: continue

        is_home = participants[0]["id"] == team_id
        stats = fix.get("statistics", [])

        shots = sot = corners = 0

        for stat in stats:
            if stat["type_id"] == 34: # Shots Total
                shots = stat["data"]["value"]
            if stat["type_id"] == 86: # Shots On Target
                sot = stat["data"]["value"]
            if stat["type_id"] == 35: # Corners
                corners = stat["data"]["value"]

        scores = fix.get("scores", [])
        goals = 0
        if scores and len(scores) >= 2:
            goals = scores[0]["score"] if is_home else scores[1]["score"]

        if shots > 0 or corners > 0:
            total_shots += shots
            total_sot += sot
            total_corners += corners
            total_goals += goals
            jogos_validos += 1

        time.sleep(0.3)

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

    if media_shots == 0 and media_corners == 0 and media_goals == 0:
        return None

    alertas = []

    if media_shots >= 18:
        linha_justa = round(media_shots * 0.5, 1)
        alertas.append(f"🎯 *Over Chutes*\n Projeção: {media_shots} | Linha justa: {linha_justa}\n Buscar: Over {linha_justa - 1} @1.85+")

    if media_corners >= 9:
        linha_justa = round(media_corners * 0.9, 1)
        alertas.append(f"🚩 *Over Escanteios*\n Projeção: {media_corners} | Linha justa: {linha_justa}\n Buscar: Over {linha_justa - 0.5} @1.90+")

    if media_goals >= 2.6:
        alertas.append(f"⚽ *Over Gols*\n Projeção: {media_goals} | Buscar: Over 2.5 @1.80+")

    if not alertas:
        return None

    nivel = "🚨🔥 ENTRADA DESREGULADA" if media_shots >= 23 else "🚨 JOGO QUENTE" if media_shots >= 20 else "⚠️ Jogo ok"

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
        "🤖 *Shot Alert Bot v6.8.3 - Sportmonks*\n\n"
        "🌍 9 ligas • 📊 Chutes, Escanteios, Gols\n"
        "🎯 Detecta linhas desreguladas\n\n"
        "/ping — Testar\n/jogos — Hoje\n/antecipados — 3 dias\n/alerta — Entradas quentes\n/diagnostico — API",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v6.8.3 online! Sportmonks grátis OK")

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
                    time.sleep(3)
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
                time.sleep(3)
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
    return 'Bot v6.8.3 Sportmonks Online'

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
