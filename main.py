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

# ─── CONFIG DE ESPORTES ───────────────────────────────────────────────────────
ESPORTES = {
    "basquete": {
        "nome": "Basquete",
        "emoji": "🏀",
        "espn_base": "https://site.api.espn.com/apis/site/v2/sports/basketball",
        "ligas": {
            "NBA": {"slug": "nba", "id": "46", "odds_sport": "basketball_nba"},
            "WNBA": {"slug": "wnba", "id": "59", "odds_sport": "basketball_wnba"},
        },
        "mercado": "totals",
        "limite_monitor": {"NBA": 215, "WNBA": 160},
        "min_ppg": 50
    },
    "futebol": {
        "nome": "Futebol",
        "emoji": "⚽",
        "espn_base": "https://site.api.espn.com/apis/site/v2/sports/soccer",
        "ligas": {
            "Brasileirão": {"slug": "bra.1", "id": "99", "odds_sport": "soccer_brazil_serie_a"},
        },
        "mercado": "totals", # Over/Under gols
        "limite_monitor": {"Brasileirão": 2.5},
        "min_ppg": 0.5
    }
}

MODO_ATUAL = "futebol" # Padrão: futebol pra testar

# ─── CACHE ────────────────────────────────────────────────────────────────────
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

# ─── THEODDSAPI ───────────────────────────────────────────────────────────────
def buscar_odds_theoddsapi(time_casa, time_fora, sport_key):
    if not ODDS_API_KEY:
        return None

    key_request = "requests_restantes_oddsapi"
    restantes = cache_get(key_request)
    if restantes == 0:
        logging.warning("Sem requests restantes na TheOddsAPI")
        return "SEM_REQUEST"

    try:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us,eu",
            "markets": "totals",
            "oddsFormat": "decimal",
            "bookmakers": "betano,bet365,kto,sportingbet,betfair"
        }
        resp = requests.get(url, params=params, timeout=12)

        if 'x-requests-remaining' in resp.headers:
            cache_set(key_request, int(resp.headers.get('x-requests-remaining')))

        if resp.status_code == 429:
            cache_set(key_request, 0)
            return "SEM_REQUEST"
        if resp.status_code!= 200:
            logging.warning(f"TheOddsAPI {sport_key} {resp.status_code}")
            return None

        dados = resp.json()

        def normalizar(nome):
            nome = nome.lower()
            for termo in ["fc", "cf", "ec", "clube", "atlético", "atletico", "los angeles", "golden state"]:
                nome = nome.replace(termo, "")
            return nome.strip().split()[0] # Pega primeira palavra

        casa_norm = normalizar(time_casa)
        fora_norm = normalizar(time_fora)

        for game in dados:
            home = normalizar(game.get("home_team", ""))
            away = normalizar(game.get("away_team", ""))

            if casa_norm in home and fora_norm in away:
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

# ─── ESPN REQUEST ─────────────────────────────────────────────────────────────
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

    # Basquete: avgPoints | Futebol: avgGoals
    stat_nome = "avgPoints" if esporte == "basquete" else "avgGoals"
    resultado = {"ppg": get_stat(stat_nome)}
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

# ─── DETECTOR ─────────────────────────────────────────────────────────────────
def formatar_alerta(event, nome_liga, esporte, antecipado=False):
    try:
        stats = analisar_confronto_espn(event, nome_liga, esporte)
        casa = stats["casa_nome"]
        fora = stats["fora_nome"]
        total_ppg = stats["total_ppg"]
        sport_key = ESPORTES[esporte]["ligas"][nome_liga]["odds_sport"]
        config = ESPORTES[esporte]

        if total_ppg < config["min_ppg"]:
            logging.info(f"Sem stats ESPN: {casa} x {fora}")
            return None

        linha_justa = round(total_ppg * 0.98, 2)
        odds_reais = buscar_odds_theoddsapi(casa, fora, sport_key)

        if odds_reais == "SEM_REQUEST":
            return "SEM_REQUEST"

        time.sleep(1)

        data_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        hora_brt = data_utc - timedelta(hours=3)

        if odds_reais:
            for odd in odds_reais:
                diferenca = linha_justa - odd["linha"]
                if diferenca >= 0.3: # 0.3 gols = value no futebol
                    nivel = "🚨🔥 ODDS DESREGULADA BR" if antecipado else "🚨💰 VALUE BR"
                    unidade = "pts" if esporte == "basquete" else "gols"
                    msg = (
                        f"{nivel}\n{'━' * 28}\n"
                        f"{config['emoji']} *{casa} x {fora}* ({nome_liga})\n"
                        f"🕐 {hora_brt.strftime('%d/%m %H:%M')} BRT\n\n"
                        f"📊 Projeção: {total_ppg} {unidade}\n"
                        f"🎯 Linha justa: Over {linha_justa}\n\n"
                        f"💵 *{odd['casa']}*: Over {odd['linha']} @ {odd['odd']}\n"
                        f"📈 Value: +{round(diferenca,2)} {unidade}"
                    )
                    return msg

        limite = config["limite_monitor"][nome_liga]
        if total_ppg >= limite:
            unidade = "pts" if esporte == "basquete" else "gols"
            return (
                f"👀 *MONITORAR*\n{'━' * 28}\n"
                f"{config['emoji']} *{casa} x {fora}* ({nome_liga})\n"
                f"🕐 {hora_brt.strftime('%d/%m %H:%M')} BRT\n\n"
                f"📊 Projeção: {total_ppg} {unidade}\n"
                f"🎯 Linha justa: Over {linha_justa}\n"
                f"⚠️ Casas BR com linha correta"
            )

        return None

    except Exception as e:
        logging.error(f"Erro formatar_alerta: {repr(e)}")
        return None

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_status = "✅ ON" if ODDS_API_KEY else "❌ OFF"
    modo = get_modo()
    config = ESPORTES[modo]
    await update.message.reply_text(
        f"🤖 *Value Bot v2.9.0*\n\n"
        f"{config['emoji']} Modo: {config['nome']}\n"
        f"💰 TheOddsAPI: {api_status}\n"
        f"🎯 Betano, Bet365, KTO\n\n"
        f"/modo futebol — Trocar pra futebol\n"
        f"/modo basquete — Trocar pra basquete\n"
        f"/ping — Testar\n/antecipados — Value 3 dias\n/alerta — Value hoje\n/debug — Testar API",
        parse_mode="Markdown"
    )

async def modo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        modo = get_modo()
        await update.message.reply_text(f"Modo atual: {ESPORTES[modo]['nome']}\n\nUse /modo futebol ou /modo basquete")
        return

    novo_modo = context.args[0].lower()
    if novo_modo not in ESPORTES:
        await update.message.reply_text("❌ Modo inválido. Use: futebol ou basquete")
        return

    set_modo(novo_modo)
    config = ESPORTES[novo_modo]
    await update.message.reply_text(f"✅ Modo trocado pra {config['emoji']} {config['nome']}")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    modo = get_modo()
    await update.message.reply_text(f"✅ Bot v2.9.0 online!\nModo: {ESPORTES[modo]['nome']}\nTheOddsAPI: {'✅' if ODDS_API_KEY else '❌'}")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ODDS_API_KEY:
        await update.message.reply_text("❌ Configure ODDS_API_KEY no Railway")
        return

    modo = get_modo()
    config = ESPORTES[modo]
    msg = await update.message.reply_text(f"🔍 Testando TheOddsAPI {config['nome']}...")

    try:
        texto = f"✅ *TheOddsAPI Status - {config['nome']}:*\n\n"

        for nome_liga, info in config["ligas"].items():
            url = f"https://api.the-odds-api.com/v4/sports/{info['odds_sport']}/odds"
            params = {"apiKey": ODDS_API_KEY, "regions": "us,eu", "markets": "totals"}
            resp = requests.get(url, params=params, timeout=10)
            dados = resp.json() if resp.status_code == 200 else []
            texto += f"{config['emoji']} {nome_liga}: {len(dados)} jogos\n"
            restantes = resp.headers.get('x-requests-remaining', 'N/A')

        texto += f"\nRequests restantes: {restantes}\n"
        if restantes == '0' or restantes == 0:
            cache_set("requests_restantes_oddsapi", 0)
            texto += "\n⚠️ Acabaram os requests do mês."

        await msg.edit_text(texto, parse_mode="Markdown")

    except Exception as e:
        await msg.edit_text(f"❌ Erro: {repr(e)}")

async def antecipados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ODDS_API_KEY:
        await update.message.reply_text("❌ Configure ODDS_API_KEY no Railway primeiro")
        return

    modo = get_modo()
    config = ESPORTES[modo]
    msg = await update.message.reply_text(f"⏰ Buscando value {config['nome']}...")

    try:
        hoje = datetime.now(timezone.utc).date()
        encontrou = False
        total_alertas = 0
        sem_request = False

        for dias in range(1, 4):
            data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")

            for nome_liga, info in config["ligas"].items():
                games = buscar_jogos_data(data_str, info["slug"], modo)

                for g in games[:2]:
                    try:
                        alert = formatar_alerta(g, nome_liga, modo, antecipado=True)
                        if alert == "SEM_REQUEST":
                            sem_request = True
                            break
                        if alert:
                            encontrou = True
                            total_alertas += 1
                            await update.message.reply_text(alert, parse_mode="Markdown")
                            await asyncio.sleep(2)
                    except Exception as e:
                        logging.error(f"Erro jogo: {repr(e)}")
                        continue
                if sem_request: break
            if sem_request: break

        if sem_request:
            await msg.edit_text("❌ Acabaram os 500 requests da TheOddsAPI do mês.\n\nVolta no dia 1º ou faz upgrade pra $29/mês.")
        elif not encontrou:
            await msg.edit_text(f"😴 Nenhuma odd desregulada nas casas BR.\n\nJogos existem mas linhas estão justas.")
        else:
            await msg.edit_text(f"✅ {total_alertas} values BR encontradas!")

    except Exception as e:
        logging.error(f"Erro /antecipados: {repr(e)}")
        await msg.edit_text(f"❌ Erro na API")

async def alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ODDS_API_KEY:
        await update.message.reply_text("❌ Configure ODDS_API_KEY no Railway")
        return

    modo = get_modo()
    config = ESPORTES[modo]
    msg = await update.message.reply_text(f"🔥 Buscando value {config['nome']} hoje...")

    try:
        hoje_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        encontrou = False

        for nome_liga, info in config["ligas"].items():
            games = buscar_jogos_data(hoje_str, info["slug"], modo)
            for g in games[:3]:
                alert = formatar_alerta(g, nome_liga, modo)
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
application.add_handler(CommandHandler("modo", modo))
application.add_handler(CommandHandler("ping", ping))
application.add_handler(CommandHandler("debug", debug))
application.add_handler(CommandHandler("antecipados", antecipados))
application.add_handler(CommandHandler("alerta", alerta))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return 'ok'

@app.route('/')
def index():
    return 'Value Bot v2.9.0 Online'

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
