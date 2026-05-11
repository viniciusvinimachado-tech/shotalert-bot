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

TOKEN = os.environ.get("BOT_TOKEN")

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

# ─── SCRAPER ODDS.COM.BR ──────────────────────────────────────────────────────
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
}

def buscar_odds_br(time_casa, time_fora):
    """Scraper odds.com.br - busca Over/Under nas casas BR"""
    try:
        # URL padrão: odds.com.br/basquete/eua/nba
        url = "https://www.odds.com.br/basquete/eua/nba/"
        resp = requests.get(url, headers=HEADERS, timeout=10)

        if resp.status_code!= 200:
            logging.warning(f"Odds.com.br status {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        odds_casas = []

        # Normaliza nomes: Lakers -> Los Angeles Lakers
        casa_norm = time_casa.lower().replace(" ", "")
        fora_norm = time_fora.lower().replace(" ", "")

        # Procura tabelas de jogos
        jogos = soup.find_all('div', class_=re.compile('event|match'))

        for jogo in jogos:
            texto_jogo = jogo.get_text().lower().replace(" ", "")

            # Match flexível: Lakers vs Thunder
            if casa_norm[:5] in texto_jogo and fora_norm[:5] in texto_jogo:

                # Pega odds de Over/Under
                linhas = jogo.find_all('div', class_=re.compile('odds|market'))
                for linha in linhas:
                    if 'over' in linha.get_text().lower() or 'mais' in linha.get_text().lower():
                        # Extrai número: Over 215.5 @ 1.90
                        texto = linha.get_text()
                        match = re.search(r'(\d+\.?\d*)\s*@\s*(\d+\.?\d*)', texto)
                        if match:
                            valor_linha = float(match.group(1))
                            odd_valor = float(match.group(2))

                            # Tenta pegar nome da casa
                            casa_aposta = "Bet365" # default
                            if 'betano' in texto.lower(): casa_aposta = "Betano"
                            elif 'kto' in texto.lower(): casa_aposta = "KTO"
                            elif 'betfair' in texto.lower(): casa_aposta = "Betfair"
                            elif 'sportingbet' in texto.lower(): casa_aposta = "Sportingbet"

                            odds_casas.append({
                                "casa": casa_aposta,
                                "linha": valor_linha,
                                "odd": odd_valor
                            })

                if odds_casas:
                    return odds_casas

        return None

    except Exception as e:
        logging.error(f"Erro scraper odds.com.br: {e}")
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
        logging.error(f"Erro salvar cache: {e}")

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
        logging.error(f"ESPN Exception: {e}")
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
        return {"ppg": 0, "rpg": 0, "apg": 0, "3pm": 0, "tpg": 0}

    stats = data["results"][0]["stats"]

    def get_stat(name):
        for s in stats:
            if s["name"] == name:
                try:
                    return float(s["value"])
                except:
                    return 0
        return 0

    resultado = {
        "ppg": get_stat("avgPoints"),
        "rpg": get_stat("avgRebounds"),
        "apg": get_stat("avgAssists"),
        "3pm": get_stat("avgThreePointFieldGoalsMade"),
        "tpg": get_stat("avgTurnovers"),
    }

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

# ─── DETECTOR COM ODDS BR REAIS ───────────────────────────────────────────────
def formatar_alerta_basket_odds_br(event, nome_liga, antecipado=False):
    try:
        stats = analisar_confronto_espn(event, nome_liga)
        casa = stats["casa_nome"]
        fora = stats["fora_nome"]
        total_ppg = stats["total_ppg"]

        if total_ppg == 0:
            return None

        linha_justa = round(total_ppg * 0.98, 1)

        # Busca odds reais no odds.com.br
        odds_reais = buscar_odds_br(casa, fora)
        await asyncio.sleep(1) # Delay pra não tomar block

        data_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        hora_brt = data_utc - timedelta(hours=3)

        if odds_reais:
            for odd in odds_reais:
                # VALUE se linha da casa tá 2+ pontos abaixo da justa
                diferenca = linha_justa - odd["linha"]
                if diferenca >= 2:
                    nivel = "🚨🔥 ODDS DESREGULADA BR" if antecipado else "🚨💰 VALUE BR ENCONTRADO"
                    msg = (
                        f"{nivel}\n{'━' * 28}\n"
                        f"🏀 *{casa} x {fora}*\n"
                        f"🕐 {hora_brt.strftime('%d/%m %H:%M')} BRT\n\n"
                        f"📊 Projeção ESPN: {total_ppg} pts\n"
                        f"🎯 Linha justa: Over {linha_justa}\n\n"
                        f"💵 *{odd['casa']}*: Over {odd['linha']} @ {odd['odd']}\n"
                        f"📈 Value: +{diferenca} pontos\n"
                        f"🔗 odds.com.br"
                    )
                    return msg

        # Se tem projeção alta mas sem value
        if total_ppg >= 215:
            return (
                f"👀 *MONITORAR - LINHA JUSTA*\n{'━' * 28}\n"
                f"🏀 *{casa} x {fora}*\n"
                f"🕐 {hora_brt.strftime('%d/%m %H:%M')} BRT\n\n"
                f"📊 Projeção: {total_ppg} pts\n"
                f"🎯 Linha justa: Over {linha_justa}\n"
                f"⚠️ Casas BR ainda com linha correta"
            )

        return None

    except Exception as e:
        logging.error(f"Erro formatar_alerta: {e}")
        return None

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Basket Alert Bot v2.7 - Odds BR*\n\n"
        "🏀 NBA • WNBA • NCAAB\n"
        "💰 Scraper odds.com.br\n"
        "🎯 Betano, Bet365, KTO, Betfair\n"
        "⏰ Antecipados 3 dias\n\n"
        "/ping — Testar\n/antecipados — Value próximos 3 dias\n/alerta — Value hoje",
        parse_mode="Markdown"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot v2.7 online!\nESPN: ✅\nOdds.com.br: ✅")

async def antecipados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏰ Buscando value nas casas BR próximos 3 dias...")

    try:
        hoje = datetime.now(timezone.utc).date()
        encontrou = False
        total_alertas = 0

        for dias in range(1, 4):
            data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")

            for nome_liga, info in LIGAS.items():
                games = buscar_jogos_data(data_str, info["slug"])

                for g in games[:1]: # 1 jogo por liga pra não tomar block
                    try:
                        alert = formatar_alerta_basket_odds_br(g, nome_liga, antecipado=True)
                        if alert:
                            encontrou = True
                            total_alertas += 1
                            await update.message.reply_text(alert, parse_mode="Markdown")
                            await asyncio.sleep(2)
                    except Exception as e:
                        logging.error(f"Erro jogo: {e}")
                        continue

        if not encontrou:
            await msg.edit_text("😴 Nenhuma odd desregulada nas casas BR.\n\nLinhas justas ou odds.com.br bloqueou.")
        else:
            await msg.edit_text(f"✅ {total_alertas} oportunidades com value BR encontradas!")

    except Exception as e:
        logging.error(f"Erro /antecipados: {e}")
        await msg.edit_text(f"❌ Erro no scraper odds.com.br")

async def alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔥 Buscando value BR nos jogos de hoje...")

    try:
        hoje_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        encontrou = False

        for nome_liga, info in LIGAS.items():
            games = buscar_jogos_data(hoje_str, info["slug"])
            for g in games[:2]:
                alert = formatar_alerta_basket_odds_br(g, nome_liga)
                if alert and "VALUE" in alert:
                    encontrou = True
                    await update.message.reply_text(alert, parse_mode="Markdown")
                    await asyncio.sleep(2)

        if not encontrou:
            await msg.edit_text("😴 Nenhuma odd desregulada hoje nas casas BR.")
        else:
            await msg.delete()

    except Exception as e:
        logging.error(f"Erro /alerta: {e}")
        await msg.edit_text(f"❌ Erro no scraper")

# ─── FLASK + WEBHOOK ───────────────────────────────────────────────────────────
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("ping", ping))
application.add_handler(CommandHandler("antecipados", antecipados))
application.add_handler(CommandHandler("alerta", alerta))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return 'ok'

@app.route('/')
def index():
    return 'Basket Bot v2.7 Odds BR Online'

def setup():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        loop.run_until_complete(application.bot.set_webhook(url=f"{RAILWAY_URL}/{TOKEN}"))

if __name__ == "__main__":
    setup()
    PORT = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=PORT)
