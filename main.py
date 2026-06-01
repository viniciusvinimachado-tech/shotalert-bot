import os
import json
import logging
import requests
import time
import asyncio
from datetime import datetime, timezone, timedelta
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
import nest_asyncio
nest_asyncio.apply()

TOKEN = os.environ.get("BOT_TOKEN")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# ─── CONFIG DE ESPORTES ───────────────────────────────────────────────────────
ESPORTES = {
    "futebol": {
        "nome": "Futebol",
        "emoji": "⚽",
        "espn_base": "https://site.api.espn.com/apis/site/v2/sports/soccer",
        "ligas": {
            "Brasileirão": {"slug": "bra.1", "id": "99", "odds_sport": "soccer_brazil_serie_a"},
            "Premier League": {"slug": "eng.1", "id": "23", "odds_sport": "soccer_epl"},
            "La Liga": {"slug": "esp.1", "id": "15", "odds_sport": "soccer_spain_la_liga"},
            "Serie A": {"slug": "ita.1", "id": "12", "odds_sport": "soccer_italy_serie_a"},
            "Bundesliga": {"slug": "ger.1", "id": "10", "odds_sport": "soccer_germany_bundesliga"},
            "Champions": {"slug": "uefa.champions", "id": "2", "odds_sport": "soccer_uefa_champs_league"},
            "Libertadores": {"slug": "conmebol.libertadores", "id": "18", "odds_sport": "soccer_conmebol_copa_libertadores"},
            "Sul-Americana": {"slug": "conmebol.sudamericana", "id": "11", "odds_sport": "soccer_conmebol_copa_sudamericana"},
        },
        "mercado": "totals",
        "limite_monitor": {
            "Brasileirão": 2.3, "Premier League": 2.8, "La Liga": 2.6, "Serie A": 2.7,
            "Bundesliga": 3.0, "Champions": 2.7, "Libertadores": 2.4, "Sul-Americana": 2.3
        },
        "min_ppg": 0.3
    }
}

MODO_ATUAL = "futebol"

# ─── CACHE + CONTROLE DIÁRIO ──────────────────────────────────────────────────
CACHE_FILE = "cache_bot.json"

def carregar_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def salvar_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        logging.error(f"Erro salvar cache: {repr(e)}")

_cache = carregar_cache()

def cache_get(key):
    item = _cache.get(key)
    if item and time.time() - item["time"] < 3600:
        return item["data"]
    return None

def cache_set(key, value):
    _cache[key] = {"data": value, "time": time.time()}
    salvar_cache(_cache)

def get_modo():
    return _cache.get("modo_atual", MODO_ATUAL)

def set_modo(modo):
    _cache["modo_atual"] = modo
    salvar_cache(_cache)

def get_alertas_hoje():
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    key = f"alertas_{hoje}"
    return _cache.get(key, {}).get("data", 0)

def add_alerta_hoje():
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    key = f"alertas_{hoje}"
    atual = get_alertas_hoje()
    cache_set(key, atual + 1)
    return atual + 1

# ─── THEODDSAPI ───────────────────────────────────────────────────────────────
def buscar_odds_theoddsapi(time_casa, time_fora, sport_key):
    if not ODDS_API_KEY:
        return None

    restantes = cache_get("requests_restantes_oddsapi")
    if restantes == 0:
        return "SEM_REQUEST"

    try:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "totals",
            "oddsFormat": "decimal",
            "bookmakers": "betano,bet365,kto,sportingbet,betfair,pinnacle"
        }
        resp = requests.get(url, params=params, timeout=12)

        if 'x-requests-remaining' in resp.headers:
            cache_set("requests_restantes_oddsapi", int(resp.headers.get('x-requests-remaining')))

        if resp.status_code == 429:
            cache_set("requests_restantes_oddsapi", 0)
            return "SEM_REQUEST"
        if resp.status_code!= 200:
            return None

        dados = resp.json()

        def normalizar(nome):
            nome = nome.lower()
            for termo in ["fc", "cf", "ec", "clube", "atlético", "atletico", "real", "ac", "sc", "de"]:
                nome = nome.replace(termo, "")
            return nome.strip()

        casa_norm = normalizar(time_casa)
        fora_norm = normalizar(time_fora)

        for game in dados:
            home = normalizar(game.get("home_team", ""))
            away = normalizar(game.get("away_team", ""))

            if (casa_norm[:6] in home or home[:6] in casa_norm) and \
               (fora_norm[:6] in away or away[:6] in fora_norm):
                odds_casas = []
                for book in game.get("bookmakers", []):
                    for market in book.get("markets", []):
                        if market["key"] == "totals":
                            for outcome in market["outcomes"]:
                                if outcome["name"] == "Over":
                                    odds_casas.append({
                                        "casa": book["title"],
                                        "linha": float(outcome["point"]),
                                        "odd": float(outcome["price"])
                                    })
                if odds_casas:
                    return odds_casas
        return None
    except Exception as e:
        logging.error(f"TheOddsAPI erro: {repr(e)}")
        return None

# ─── ESPN ─────────────────────────────────────────────────────────────────────
def espn_request(endpoint, esporte):
    url = f"{ESPORTES[esporte]['espn_base']}/{endpoint}"
    try:
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logging.error(f"ESPN Exception: {repr(e)}")
        return None

def buscar_jogos_data(data_str, liga_slug, esporte):
    key = f"jogos_{data_str}_{liga_slug}_{esporte}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    data_espn = data_str.replace("-", "")
    endpoint = f"{liga_slug}/scoreboard?dates={data_espn}"
    data = espn_request(endpoint, esporte)

    result = data.get("events", []) if data else []
    logging.info(f"ESPN {esporte} {liga_slug} {data_str}: {len(result)} jogos")
    cache_set(key, result)
    return result

def buscar_stats_time_espn(team_id, liga_slug, esporte):
    key = f"espn_team_stats_{team_id}_{liga_slug}_{esporte}"
    cached = cache_get(key)
    if cached: return cached

    endpoint = f"{liga_slug}/teams/{team_id}/statistics"
    data = espn_request(endpoint, esporte)

    if not data or not data.get("results"):
        return {"ppg": 0}

    stats = data["results"][0]["stats"]

    def get_stat(name):
        for s in stats:
            if s["name"] == name:
                try:
                    return float(s["value"])
                except:
                    return 0
        return 0

    resultado = {"ppg": get_stat("avgGoals")}
    cache_set(key, resultado)
    return resultado

def analisar_confronto_espn(event, nome_liga, esporte):
    liga_slug = ESPORTES[esporte]["ligas"][nome_liga]["slug"]
    comp = event["competitions"][0]
    casa = comp["competitors"][0]
    fora = comp["competitors"][1]

    stats_casa = buscar_stats_time_espn(casa["id"], liga_slug, esporte)
    stats_fora = buscar_stats_time_espn(fora["id"], liga_slug, esporte)

    return {
        "total_ppg": round(stats_casa["ppg"] + stats_fora["ppg"], 2),
        "casa_nome": casa["team"]["displayName"],
        "fora_nome": fora["team"]["displayName"],
    }

# ─── MOTOR CONSERVADOR 1.80 ───────────────────────────────────────────────────
def formatar_alerta(event, nome_liga, esporte, antecipado=False):
    if get_alertas_hoje() >= 3:
        return "LIMITE_DIARIO"

    try:
        stats = analisar_confronto_espn(event, nome_liga, esporte)
        casa = stats["casa_nome"]
        fora = stats["fora_nome"]
        total_ppg = stats["total_ppg"]
        sport_key = ESPORTES[esporte]["ligas"][nome_liga]["odds_sport"]
        config = ESPORTES[esporte]

        if total_ppg < config["min_ppg"]:
            return None

        linha_justa = round(total_ppg * 0.98, 2)
        odds_reais = buscar_odds_theoddsapi(casa, fora, sport_key)

        if odds_reais == "SEM_REQUEST":
            return "SEM_REQUEST"

        time.sleep(1.5)

        if odds_reais:
            for odd in odds_reais:
                diferenca = linha_justa - odd["linha"]
                # CONSERVADOR: Odd 1.70-1.85 + Value mín 0.20 gols
                if 1.70 <= odd["odd"] <= 1.85 and diferenca >= 0.20:
                    add_alerta_hoje()
                    hora_brt = datetime.fromisoformat(event["date"].replace("Z", "+00:00")) - timedelta(hours=3)
                    winrate = round(1 / odd["odd"] * 100 + 5, 1)
                    msg = (
                        f"🛡️ *CONSERVADOR {get_alertas_hoje()}/3*\n{'━' * 28}\n"
                        f"{config['emoji']} *{casa} x {fora}* ({nome_liga})\n"
                        f"🕐 {hora_brt.strftime('%d/%m %H:%M')} BRT\n\n"
                        f"📊 Projeção: {total_ppg} gols\n"
                        f"🎯 Linha justa: Over {linha_justa}\n\n"
                        f"💵 *{odd['casa']}*: Over {odd['linha']} @ {odd['odd']}\n"
                        f"📈 Value: +{round(diferenca,2)} gols\n"
                        f"🎲 Acerto esperado: ~{winrate}%\n"
                        f"💰 Stake sugerida: R$100"
                    )
                    return msg
        return None
    except Exception as e:
        logging.error(f"Erro formatar_alerta: {repr(e)}")
        return None

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_status = "✅ ON" if ODDS_API_KEY else "❌ OFF"
    modo = get_modo()
    config = ESPORTES[modo]
    ligas = ", ".join(config["ligas"].keys())
    restantes = cache_get("requests_restantes_oddsapi") or "500"
    alertas = get_alertas_hoje()

    await update.message.reply_text(
        f"🤖 *Value Bot v3.3 Conservador*\n\n"
        f"{config['emoji']} Modo: {config['nome']}\n"
        f"📋 Ligas: {len(config['ligas'])}\n"
        f"💰 TheOddsAPI: {api_status} | Requests: {restantes}\n"
        f"🛡️ Filtro: Odd 1.70-1.85 | Limite: {alertas}/3 hoje\n\n"
        f"/antecipados — Buscar 3 values do dia\n"
        f"/alerta — Value pra hoje\n"
        f"/jogos — Ver jogos sem gastar API\n"
        f"/status — Requests + alertas\n"
        f"/ping — Testar",
        parse_mode="Markdown"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    restantes = cache_get("requests_restantes_oddsapi")
    alertas = get_alertas_hoje()
    await update.message.reply_text(
        f"📊 *Status*\n\n"
        f"Requests TheOddsAPI: {restantes if restantes is not None else '500'}/500\n"
        f"Alertas hoje: {alertas}/3\n"
        f"Filtro: Odd 1.70-1.85\n"
        f"Value mínimo: +0.20 gols"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Bot v3.3 online!\nAlertas hoje: {get_alertas_hoje()}/3")

async def jogos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    modo = get_modo()
    config = ESPORTES[modo]
    msg = await update.message.reply_text(f"📅 Buscando jogos {config['nome']} hoje...")

    try:
        hoje_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        texto = f"📅 *Jogos {config['nome']} hoje:*\n\n"
        total = 0

        for nome_liga, info in config["ligas"].items():
            games = buscar_jogos_data(hoje_str, info["slug"], modo)
            if games:
                texto += f"*{nome_liga}:* {len(games)} jogos\n"
                total += len(games)
                for g in games[:2]:
                    comp = g["competitions"][0]
                    casa = comp["competitors"][0]["team"]["displayName"]
                    fora = comp["competitors"][1]["team"]["displayName"]
                    hora = datetime.fromisoformat(g["date"].replace("Z", "+00:00")) - timedelta(hours=3)
                    texto += f" • {hora.strftime('%H:%M')} {casa} x {fora}\n"
                texto += "\n"

        if total == 0:
            texto += "😴 Nenhum jogo hoje nas ligas cadastradas."

        await msg.edit_text(texto, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Erro: {repr(e)}")

async def antecipados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_alertas_hoje() >= 3:
        await update.message.reply_text("✅ Limite de 3 alertas hoje já atingido. Volta amanhã.")
        return

    if not ODDS_API_KEY:
        await update.message.reply_text("❌ Configure ODDS_API_KEY no Railway")
        return

    modo = get_modo()
    config = ESPORTES[modo]
    restantes = cache_get("requests_restantes_oddsapi")
    msg = await update.message.reply_text(f"⏰ Buscando 3 values 1.80... Requests: {restantes}")

    try:
        hoje = datetime.now(timezone.utc).date()
        encontrou = 0

        for dias in range(1, 4):
            if get_alertas_hoje() >= 3: break
            data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")

            for nome_liga, info in config["ligas"].items():
                if get_alertas_hoje() >= 3: break
                games = buscar_jogos_data(data_str, info["slug"], modo)

                for g in games[:2]:
                    if get_alertas_hoje() >= 3: break
                    alert = formatar_alerta(g, nome_liga, modo, antecipado=True)
                    if alert == "SEM_REQUEST":
                        await msg.edit_text("❌ Acabaram os 500 requests do mês.\nVolta dia 1º.")
                        return
                    if alert and alert!= "LIMITE_DIARIO":
                        encontrou += 1
                        await update.message.reply_text(alert, parse_mode="Markdown")
                        await asyncio.sleep(2)

        if encontrou == 0:
            await msg.edit_text("😴 Nenhum value conservador 1.70-1.85 hoje.\nLinhas estão justas ou sem odds.")
        else:
            await msg.edit_text(f"✅ {encontrou} alertas enviados. Limite: {get_alertas_hoje()}/3")

    except Exception as e:
        logging.error(f"Erro /antecipados: {repr(e)}")
        await msg.edit_text(f"❌ Erro na API")

async def alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_alertas_hoje() >= 3:
        await update.message.reply_text("✅ Limite de 3 alertas hoje já atingido.")
        return

    if not ODDS_API_KEY:
        await update.message.reply_text("❌ Configure ODDS_API_KEY")
        return

    modo = get_modo()
    config = ESPORTES[modo]
    msg = await update.message.reply_text(f"🔥 Buscando value 1.80 pra hoje...")

    try:
        hoje_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        encontrou = False

        for nome_liga, info in config["ligas"].items():
            if get_alertas_hoje() >= 3: break
            games = buscar_jogos_data(hoje_str, info["slug"], modo)
            for g in games[:2]:
                if get_alertas_hoje() >= 3: break
                alert = formatar_alerta(g, nome_liga, modo)
                if alert and alert not in ["SEM_REQUEST", "LIMITE_DIARIO"]:
                    encontrou = True
                    await update.message.reply_text(alert, parse_mode="Markdown")
                    await asyncio.sleep(2)

        if not encontrou:
            await msg.edit_text("😴 Nenhum value conservador hoje.")
        else:
            await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ Erro: {repr(e)}")

# ─── FLASK + WEBHOOK ───────────────────────────────────────────────────────────
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("ping", ping))
application.add_handler(CommandHandler("jogos", jogos))
application.add_handler(CommandHandler("antecipados", antecipados))
application.add_handler(CommandHandler("alerta", alerta))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return 'ok'

@app.route('/')
def index():
    return 'Value Bot v3.3 Conservador Online'

async def setup():
    await application.initialize()
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        await application.bot.set_webhook(url=f"{RAILWAY_URL}/{TOKEN}")
        logging.info(f"Webhook set: {RAILWAY_URL}/{TOKEN}")

if __name__ == "__main__":
    asyncio.run(setup())
    PORT = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=PORT)
