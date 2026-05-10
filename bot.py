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
CHAT_ID = os.environ.get("CHAT_ID")
API_KEY = os.environ.get("API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── API-FOOTBALL ─────────────────────────────────────────────────────────────
HEADERS = {
    "x-apisports-key": API_KEY,
    "x-apisports-host": "v3.football.api-sports.io"
}
BASE_URL = "https://v3.football.api-sports.io"
session = requests.Session()
session.headers.update(HEADERS)

# ─── SEASON UTC ───────────────────────────────────────────────────────────────
def get_season(league_id):
    hoje = datetime.now(timezone.utc).date()
    ano = hoje.year
    mes = hoje.month

    ligas_europa = ["2", "3", "848", "39", "140", "135", "78", "61", "88", "94"]
    ligas_anuais = ["71", "73", "13", "11", "128"]
    ligas_mls = ["253", "262"]

    if str(league_id) in ligas_europa:
        return str(ano - 1) if mes < 7 else str(ano)
    if str(league_id) in ligas_mls:
        return str(ano) if mes >= 2 else str(ano - 1)
    if str(league_id) in ligas_anuais:
        return str(ano)
    return str(ano)

LIGAS = {
    "Brasileirao": {"id": "71", "season": get_season("71")},
    "Copa do Brasil": {"id": "73", "season": get_season("73")},
    "Libertadores": {"id": "13", "season": get_season("13")},
    "Sul-Americana": {"id": "11", "season": get_season("11")},
    "Champions League": {"id": "2", "season": get_season("2")},
    "Europa League": {"id": "3", "season": get_season("3")},
    "Conference League": {"id": "848", "season": get_season("848")},
    "Premier League": {"id": "39", "season": get_season("39")},
    "La Liga": {"id": "140", "season": get_season("140")},
    "Serie A Italia": {"id": "135", "season": get_season("135")},
    "Bundesliga": {"id": "78", "season": get_season("78")},
    "Ligue 1": {"id": "61", "season": get_season("61")},
    "Eredivisie": {"id": "88", "season": get_season("88")},
    "Liga Portugal": {"id": "94", "season": get_season("94")},
    "MLS": {"id": "253", "season": get_season("253")},
    "Liga MX": {"id": "262", "season": get_season("262")},
    "Argentina Liga": {"id": "128", "season": get_season("128")},
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

# ─── REQUEST API ──────────────────────────────────────────────────────────────
def api_request(endpoint, params):
    url = f"{BASE_URL}/{endpoint}"
    try:
        resp = session.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("errors"):
            logging.error(f"API ERR {endpoint}: {data['errors']}")
        return data.get("response", [])
    except Exception as e:
        logging.error(f"Exception {endpoint}: {e}")
        return []

def buscar_jogos_data(data_str, league_id, season):
    key = f"jogos_{data_str}_{league_id}_{season}"
    cached = cache_get(key)
    if cached is not None:
        logging.info(f"CACHE HIT: L{league_id} {data_str}")
        return cached

    logging.info(f"REQ: L{league_id} S{season} D{data_str}")
    params = {"date": data_str, "league": league_id, "season": season, "timezone": "UTC"}
    result = api_request("fixtures", params)
    logging.info(f"API {league_id} {season} {data_str}: {len(result)} jogos")
    cache_set(key, result)
    return result

def buscar_stats(team_id, league_id, season):
    key = f"stats_{team_id}_{league_id}_{season}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    params = {"team": team_id, "league": league_id, "season": season}
    data = api_request("teams/statistics", params)
    data = data[0] if data else {}

    jogos = data.get("fixtures", {}).get("played", {}).get("total", 1) or 1
    shots = data.get("shots", {})
    total = shots.get("total", {}).get("total", 0) or 0
    no_alvo = shots.get("on", {}).get("total", 0) or 0
    media = round(total / jogos, 1)
    media_alvo = round(no_alvo / jogos, 1)
    ofensividade = min(round((media / 20) * 100), 100)
    result = {"media": media, "media_alvo": media_alvo, "ofensividade": ofensividade, "jogos": jogos}
    cache_set(key, result)
    return result

def buscar_artilheiros(team_id, league_id, season):
    key = f"art_{team_id}_{league_id}_{season}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    params = {"team": team_id, "league": league_id, "season": season}
    players = api_request("players", params)

    finalizadores = []
    for p in players:
        stats = p.get("statistics", [{}])[0]
        shots = stats.get("shots", {})
        total_shots = shots.get("total") or 0
        on_target = shots.get("on") or 0
        goals = stats.get("goals", {}).get("total") or 0
        name = p.get("player", {}).get("name", "")
        if total_shots >= 3:
            finalizadores.append({
                "nome": name, "chutes": total_shots,
                "no_alvo": on_target, "gols": goals,
            })
    finalizadores.sort(key=lambda x: x["chutes"], reverse=True)
    result = finalizadores[:3]
    cache_set(key, result)
    return result

# ─── ANÁLISE ──────────────────────────────────────────────────────────────────
def calcular_confianca(total_media, importancia, ofens_casa, ofens_fora, jogos_casa, jogos_fora):
    base = min(total_media / 35 * 55, 55)
    bonus_imp = importancia * 2
    bonus_ofens = ((ofens_casa + ofens_fora) / 200) * 8
    bonus_amostra = min(((jogos_casa + jogos_fora) / 2) / 30 * 5, 5)
    return min(round(base + bonus_imp + bonus_ofens + bonus_amostra), 99)

def calcular_importancia(fixture):
    rodada = fixture.get("league", {}).get("round", "")
    liga_id = str(fixture.get("league", {}).get("id", ""))
    if "Final" in rodada: return 10
    if "Semi" in rodada: return 9
    if "Quarter" in rodada: return 8
    if liga_id in ["2", "13"]: return 9
    if liga_id in ["3", "11", "73", "848"]: return 8
    return 7

def formatar_alerta(fixture, stats_casa, stats_fora, fin_casa, fin_fora, dias_restantes=0):
    casa = fixture["teams"]["home"]["name"]
    fora = fixture["teams"]["away"]["name"]
    hora = fixture["fixture"]["date"][11:16]
    data_jogo = fixture["fixture"]["date"][:10]
    liga = fixture["league"]["name"]
    total_media = round(stats_casa["media"] + stats_fora["media"], 1)
    importancia = calcular_importancia(fixture)
    confianca = calcular_confianca(
        total_media, importancia,
        stats_casa["ofensividade"], stats_fora["ofensividade"],
        stats_casa["jogos"], stats_fora["jogos"]
    )

    prefixos = {0: "🚨 *ALERTA HOJE*", 1: "⚡ *ALERTA — AMANHÃ*", 2: "📅 *ANTECIPADO — 2 DIAS*", 3: "📅 *ANTECIPADO — 3 DIAS*"}
    janelas = {1: "\n⏰ *Entrar hoje garante odd melhor!*", 2: "\n⏰ *Odd desregulada — janela aberta!*", 3: "\n⏰ *Melhor momento — odd no pico!*"}
    prefixo = prefixos.get(dias_restantes, "🚨 *ALERTA*")
    janela = janelas.get(dias_restantes, "")
    linha = round(total_media - 3, 1)

    fin_casa_txt = "\n".join([f" ⚡ {f['nome']}: {f['chutes']} ch | {f['no_alvo']} alvo | {f['gols']} gols" for f in fin_casa]) or " Dados indisponíveis"
    fin_fora_txt = "\n".join([f" ⚡ {f['nome']}: {f['chutes']} ch | {f['no_alvo']} alvo | {f['gols']} gols" for f in fin_fora]) or " Dados indisponíveis"

    msg = (
        f"{prefixo}\n{'━' * 28}\n🏆 {liga}\n⚽ *{casa} x {fora}*\n🕐 {data_jogo} às {hora}\n\n"
        f"📊 *Finalizações/jogo:*\n🏠 {casa}: *{stats_casa['media']}* | {stats_casa['media_alvo']} no alvo\n"
        f"✈️ {fora}: *{stats_fora['media']}* | {stats_fora['media_alvo']} no alvo\n"
        f"📈 Combinado: *{total_media} fin/jogo*\n\n"
        f"🎯 *Principais finalizadores:*\n🏠 {casa}:\n{fin_casa_txt}\n✈️ {fora}:\n{fin_fora_txt}\n\n"
        f"💪 Ofensividade:\n🏠 {stats_casa['ofensividade']}% | ✈️ {stats_fora['ofensividade']}%\n\n"
        f"🔥 Importância: {importancia}/10\n✅ *Confiança: {confianca}%*\n\n"
        f"💰 *Mercado: Over {linha} finalizações*{janela}"
    )
    return msg, confianca

def analisar_jogos(fixtures, league_id, season, dias_restantes=0):
    alertas = []
    for f in fixtures[:3]:
        id_casa = f["teams"]["home"]["id"]
        id_fora = f["teams"]["away"]["id"]
        stats_casa = buscar_stats(id_casa, league_id, season)
        stats_fora = buscar_stats(id_fora, league_id, season)
        total = stats_casa["media"] + stats_fora["media"]
        if total >= 18:
            fin_casa = buscar_artilheiros(id_casa, league_id, season)
            fin_fora = buscar_artilheiros(id_fora, league_id, season)
            msg, confianca = formatar_alerta(f, stats_casa, stats_fora, fin_casa, fin_fora, dias_restantes)
            if confianca >= 75:
                alertas.append((msg, confianca))
    alertas.sort(key=lambda x: x[1], reverse=True)
    return alertas[:3]

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
def start(update, context):
    update.message.reply_text(
        "🤖 *Finalizações Bot v6.4*\n\n"
        "🌍 17 ligas • 🎯 Filtro 75%+ • 📊 Cache 1h\n\n"
        "/ping — Testar bot\n/jogos — Jogos hoje\n/alerta — Alertas hoje\n"
        "/antecipados — Próximos 3 dias\n/diagnostico — Testar API\n/testapi — Teste direto",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v6.4 online! UTC | Webhook OK")

def testapi(update, context):
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    season = get_season("39")
    params = {"date": hoje, "league": "39", "season": season, "timezone": "UTC"}
    data = api_request("fixtures", params)
    update.message.reply_text(f"🔬 *Teste API*\nData: {hoje} UTC\nSeason: {season}\nPremier League: {len(data)} jogos", parse_mode="Markdown")

def diagnostico(update, context):
    update.message.reply_text("🔬 Testando todas as ligas...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    resultado = ""
    total = 0
    for nome, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        count = len(fixtures)
        if count > 0:
            resultado += f"✅ {nome}: {count}\n"
            total += count
        else:
            resultado += f"⚪ {nome}: 0\n"
    update.message.reply_text(f"📊 *Diagnóstico — {hoje} UTC*\n\n{resultado}\n*Total: {total} jogos*", parse_mode="Markdown")

def jogos(update, context):
    update.message.reply_text("🔍 Buscando jogos de hoje...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        if not fixtures: continue
        encontrou = True
        msg = f"⚽ *{nome_liga} S{info['season']}:*\n"
        for f in fixtures[:5]:
            casa = f["teams"]["home"]["name"]
            fora = f["teams"]["away"]["name"]
            hora = f["fixture"]["date"][11:16]
            msg += f"🕐 {hora} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhum jogo hoje na API.")

def alerta(update, context):
    update.message.reply_text("📊 Analisando entradas de hoje...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        if not fixtures: continue
        alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=0)
        for msg, confianca in alertas:
            encontrou = True
            update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("🎯 Nenhuma entrada com 75%+ hoje.")

def antecipados(update, context):
    update.message.reply_text("📅 Buscando próximos 3 dias...")
    hoje = datetime.now(timezone.utc).date()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            fixtures = buscar_jogos_data(data_str, info["id"], info["season"])
            if not fixtures: continue
            alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=dias)
            for msg, confianca in alertas:
                encontrou = True
                update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhuma oportunidade antecipada.")

# ─── FLASK + WEBHOOK ──────────────────────────────────────────────────────────
app = Flask(__name__)
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, None, workers=0, use_context=True)

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("ping", ping))
dp.add_handler(CommandHandler("jogos", jogos))
dp.add_handler(CommandHandler("alerta", alerta))
dp.add_handler(CommandHandler("antecipados", antecipados))
dp.add_handler(CommandHandler("diagnostico", diagnostico))
dp.add_handler(CommandHandler("testapi", testapi))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return 'ok'

@app.route('/')
def index():
    return 'Bot v6.4 Online'

@app.route('/health')
def health():
    return 'ok', 200

@app.route('/setwebhook')
def setwebhook():
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    webhook_url = f"{RAILWAY_URL}/{TOKEN}"
    bot.set_webhook(url=webhook_url)
    return f'Webhook setado: {webhook_url}'

if __name__ == "__main__":
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        bot.set_webhook(url=f"{RAILWAY_URL}/{TOKEN}")
        logging.info("Webhook setado")
    PORT = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=PORT)
