import os
import json
import logging
import requests
import time
import asyncio
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from bs4 import BeautifulSoup
import nest_asyncio
nest_asyncio.apply()

TOKEN = os.environ.get("BOT_TOKEN")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── ESPN API ─────────────────────────────────────────────────────────────────
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
LIGAS = {
    "NBA": {"slug": "nba", "id": "46"},
    "WNBA": {"slug": "wnba", "id": "59"},
    "NCAAB": {"slug": "mens-college-basketball", "id": "41"},
}

# ─── THEODDSAPI ───────────────────────────────────────────────────────────────
def buscar_odds_theoddsapi(time_casa, time_fora):
    if not ODDS_API_KEY:
        logging.warning("ODDS_API_KEY não configurada")
        return None
    try:
        url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "br",
            "markets": "totals",
            "oddsFormat": "decimal"
        }
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code!= 200:
            logging.warning(f"TheOddsAPI {resp.status_code}")
            return None

        dados = resp.json()
        for game in dados:
            home = game.get("home_team", "").lower()
            away = game.get("away_team", "").lower()

            if (time_casa.lower()[:4] in home) and (time_fora.lower()[:4] in away):
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
                    logging.info(f"Odds {time_casa} x {time_fora}: {len(odds_casas)} casas")
                    return odds_casas
        return None
    except Exception as e:
        logging.error(f"TheOddsAPI erro: {repr(e)}")
        return None

# ─── CACHE ────────────────────────────────────────────────────────────────────
CACHE_FILE = "cache_espn.json"

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

# ─── ESPN REQUEST ─────────────────────────────────────────────────────────────
def espn_request(endpoint):
    url = f"{ESPN_BASE}/{endpoint}"
    try:
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logging.error(f"ESPN Exception: {repr(e)}")
        return None

def buscar_jogos_data(data_str, liga_slug):
    key = f"jogos_{data_str}_{liga_slug}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    data_espn = data_str.replace("-", "")
    endpoint = f"{liga_slug}/scoreboard?dates={data_espn}"
    data = espn_request(endpoint)

    result = data.get("events", []) if data else []
    logging.info(f"ESPN {liga_slug} {data_str}: {len(result)} jogos")
    cache_set(key, result)
    return result

def buscar_stats_time_espn(team_id, liga_slug):
    key = f"espn_team_stats_{team_id}_{liga_slug}"
    cached = cache_get(key)
    if cached: return cached

    endpoint = f"{liga_slug}/teams/{team_id}/statistics"
    data = espn_request(endpoint)

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

    resultado = {"ppg": get_stat("avgPoints")}
    cache_set(key, resultado)
    return resultado

def analisar_confronto_espn(event, nome_liga):
    liga_slug = LIGAS[nome_liga]["slug"]
    comp = event["competitions"][0]
    casa = comp["competitors"][0]
    fora = comp["competitors"][1]

    stats_casa = buscar_stats_time_espn(casa["id"], liga_slug)
    stats_fora = buscar_stats_time_espn(fora["id"], liga_slug)

    return {
        "total_ppg": round(stats_casa["ppg"] + stats_fora["ppg"], 1),
        "casa_nome": casa["team"]["displayName"],
        "fora_nome": fora["team"]["displayName"],
    }

# ─── DETECTOR THEODDSAPI ──────────────────────────────────────────────────────
def formatar_alerta_basket_odds(event, nome_liga, antecipado=False):
    try:
        stats = analisar_confronto_espn(event, nome_liga)
        casa = stats["casa_nome"]
        fora = stats["fora_nome"]
        total_ppg = stats["total_ppg"]

        if total_ppg < 100:
            logging.info(f"Sem stats: {casa} x {fora}")
            return None

        linha_justa = round(total_ppg * 0.98, 1)

        odds_reais = buscar_odds_theoddsapi(casa, fora)
        time.sleep(1)

        data_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        hora_brt = data_utc - timedelta(hours=3)

        if odds_reais:
            for odd in odds_reais:
                diferenca = linha_justa - odd["linha"]
                if diferenca >= 2:
                    nivel = "🚨🔥 ODDS DESREGULADA BR" if antecipado else "🚨💰 VALUE BR"
                    msg = (
                        f"{nivel}\n{'━' * 28}\n"
                        f"🏀 *{casa} x {fora}*\n"
                        f"🕐 {hora_brt.strftime('%d/%m %H:%M')} BRT\n\n"
                        f"📊 Projeção: {total_ppg} pts\n"
                        f"🎯 Linha justa: Over {linha_justa}\n\n"
                        f"💵 *{odd['casa']}*: Over {odd['linha']} @ {odd['odd']}\n"
                        f"📈 Value: +{diferenca} pontos"
                    )
                    return msg

        if total_ppg >= 215:
            return (
                f"👀 *MONITORAR*\n{'━' * 28}\n"
                f"🏀 *{casa} x {fora}*\n"
                f"🕐 {hora_brt.strftime('%d/%m %H:%M')} BRT\n\n"
                f"📊 Projeção: {total_ppg} pts\n"
                f"🎯 Linha justa: Over {linha_justa}\n"
                f"⚠️ Casas BR com linha correta"
            )

        return None

    except Exception as e:
        logging.error(f"Erro formatar_alerta_odds: {repr(e)}")
        return None

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_status = "✅ ON" if ODDS_API_KEY else "❌ OFF"
    await update.message.reply_text(
        f"🤖 *Basket Alert Bot v2.8.2*\n\n"
        f"🏀 NBA • WNBA • NCAAB\n"
        f"💰 TheOddsAPI: {api_status}\n"
        f"🎯 Betano, Bet365, KTO\n\n"
        f"/ping — Testar\n/antecipados — Value 3 dias\n/alerta — Value hoje",
        parse_mode="Markdown"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Bot v2.8.2 online!\nTheOddsAPI: {'✅' if ODDS_API_KEY else '❌'}")

async def antecipados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ODDS_API_KEY:
        await update.message.reply_text("❌ Configure ODDS_API_KEY no Railway primeiro")
        return

    msg = await update.message.reply_text("⏰ Buscando value nas casas BR...")

    try:
        hoje = datetime.now(timezone.utc).date()
        encontrou = False
        total_alertas = 0

        for dias in range(1, 4):
            data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")

            for nome_liga, info in LIGAS.items():
                games = buscar_jogos_data(data_str, info["slug"])

                for g in games[:1]:
                    try:
                        alert = formatar_alerta_basket_odds(g, nome_liga, antecipado=True)
                        if alert:
                            encontrou = True
                            total_alertas += 1
                            await update.message.reply_text(alert, parse_mode="Markdown")
                            await asyncio.sleep(2)
                    except Exception as e:
                        logging.error(f"Erro jogo: {repr(e)}")
                        continue

        if not encontrou:
            await msg.edit_text("😴 Nenhuma odd desregulada nas casas BR.")
        else:
            await msg.edit_text(f"✅ {total_alertas} values BR encontradas!")

    except Exception as e:
        logging.error(f"Erro /antecipados: {repr(e)}")
        await msg.edit_text(f"❌ Erro na API")

async def alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ODDS_API_KEY:
        await update.message.reply_text("❌ Configure ODDS_API_KEY no Railway")
        return

    msg = await update.message.reply_text("🔥 Buscando value BR hoje...")

    try:
        hoje_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        encontrou = False

        for nome_liga, info in LIGAS.items():
            games = buscar_jogos_data(hoje_str, info["slug"])
            for g in games[:2]:
                alert = formatar_alerta_basket_odds(g, nome_liga)
                if alert and "VALUE" in alert:
                    encontrou = True
                    await update.message.reply_text(alert, parse_mode="Markdown")
                    await asyncio.sleep(2)

        if not encontrou:
            await msg.edit_text("😴 Nenhuma odd desregulada hoje.")
        else:
            await msg.delete()

    except Exception as e:
        logging.error(f"Erro /alerta: {repr(e)}")
        await msg.edit_text(f"❌ Erro na API")

# ─── FLASK + WEBHOOK ───────────────────────────────────────────────────────────
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("ping", ping))
application.add_handler(CommandHandler("antecipados", antecipados))
application.add_handler(CommandHandler("alerta", alerta))

@app.route(f'/{TOKEN}', methods=['POST'])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return 'ok'

@app.route('/')
def index():
    return 'Basket Bot v2.8.2 Online'

async def setup():
    await application.initialize()
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        await application.bot.set_webhook(url=f"{RAILWAY_URL}/{TOKEN}")

if __name__ == "__main__":
    asyncio.run(setup())
    PORT = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=PORT)
