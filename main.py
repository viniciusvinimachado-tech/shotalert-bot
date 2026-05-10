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

TOKEN = os.environ.get("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── ESPN API ─────────────────────────────────────────────────────────────────
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball"

LIGAS = {
    "NBA": {"slug": "nba", "id": "46"},
    "WNBA": {"slug": "wnba", "id": "59"},
    "NCAAB": {"slug": "mens-college-basketball", "id": "41"},
}

# ─── CACHE ────────────────────────────────────────────────────────────────────
CACHE_FILE = "cache_espn.json"

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

# ─── ESPN REQUEST ─────────────────────────────────────────────────────────────
def espn_request(endpoint, base="site"):
    url = f"{ESPN_BASE}/{endpoint}" if base == "site" else f"{ESPN_CORE}/{endpoint}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logging.warning(f"ESPN status {resp.status_code}: {endpoint}")
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
    time.sleep(0.3)
    return result

def buscar_stats_time_espn(team_id, liga_slug):
    key = f"espn_team_stats_{team_id}_{liga_slug}"
    cached = cache_get(key)
    if cached: return cached

    endpoint = f"{liga_slug}/teams/{team_id}/statistics"
    data = espn_request(endpoint)

    if not data or not data.get("results"):
        return {"ppg": 0, "rpg": 0, "apg": 0, "3pm": 0, "tpg": 0, "jogos": 0}

    stats = data["results"][0]["stats"]

    def get_stat(name):
        for s in stats:
            if s["name"] == name:
                return float(s["value"])
        return 0

    resultado = {
        "ppg": get_stat("avgPoints"),
        "rpg": get_stat("avgRebounds"),
        "apg": get_stat("avgAssists"),
        "3pm": get_stat("avgThreePointFieldGoalsMade"),
        "tpg": get_stat("avgTurnovers"),
        "jogos": int(get_stat("gamesPlayed"))
    }

    cache_set(key, resultado)
    logging.info(f"ESPN Stats Team {team_id}: {resultado}")
    time.sleep(0.2)
    return resultado

def buscar_top_jogadores_espn(team_id, liga_slug):
    key = f"espn_players_{team_id}_{liga_slug}"
    cached = cache_get(key)
    if cached: return cached

    endpoint = f"{liga_slug}/teams/{team_id}?enable=roster,stats"
    data = espn_request(endpoint)

    if not data or not data.get("team", {}).get("athletes"):
        return []

    jogadores = []
    for ath in data["team"]["athletes"][:10]:
        if not ath.get("statistics"):
            continue

        stats = ath["statistics"]
        min_jogos = float(stats.get("gamesPlayed", 0))
        if min_jogos < 5:
            continue

        jogadores.append({
            "nome": ath.get("displayName", "N/A"),
            "pos": ath.get("position", {}).get("abbreviation", ""),
            "ppg": float(stats.get("avgPoints", 0)),
            "rpg": float(stats.get("avgRebounds", 0)),
            "apg": float(stats.get("avgAssists", 0)),
            "3pm": float(stats.get("avgThreePointFieldGoalsMade", 0)),
            "min": float(stats.get("avgMinutes", 0)),
            "jogos": int(min_jogos)
        })

    jogadores.sort(key=lambda x: x["ppg"], reverse=True)
    cache_set(key, jogadores[:6])
    time.sleep(0.2)
    return jogadores[:6]

def analisar_confronto_espn(event, nome_liga):
    liga_slug = LIGAS[nome_liga]["slug"]
    comp = event["competitions"][0]
    casa = comp["competitors"][0]
    fora = comp["competitors"][1]

    stats_casa = buscar_stats_time_espn(casa["id"], liga_slug)
    stats_fora = buscar_stats_time_espn(fora["id"], liga_slug)

    players_casa = buscar_top_jogadores_espn(casa["id"], liga_slug)
    players_fora = buscar_top_jogadores_espn(fora["id"], liga_slug)

    return {
        "total_ppg": round(stats_casa["ppg"] + stats_fora["ppg"], 1),
        "total_rpg": round(stats_casa["rpg"] + stats_fora["rpg"], 1),
        "total_apg": round(stats_casa["apg"] + stats_fora["apg"], 1),
        "total_3pm": round(stats_casa["3pm"] + stats_fora["3pm"], 1),
        "casa_ppg": stats_casa["ppg"],
        "fora_ppg": stats_fora["ppg"],
        "casa_tpg": stats_casa["tpg"],
        "fora_tpg": stats_fora["tpg"],
        "players_casa": players_casa,
        "players_fora": players_fora,
        "casa_nome": casa["team"]["displayName"],
        "fora_nome": fora["team"]["displayName"],
    }

# ─── DETECTOR DE ODDS DESREGULADAS ────────────────────────────────────────────
def analisar_props_jogador(player):
    alertas = []
    if player["ppg"] >= 22 and player["min"] >= 30:
        linha_justa = round(player["ppg"] * 0.95, 1)
        alertas.append(f"⭐ *{player['nome']} Over {linha_justa - 1.5} Pontos*\n Projeção: {player['ppg']} pts | {player['min']} min\n Linha justa: {linha_justa}")
    if player["rpg"] >= 8 and player["min"] >= 28:
        linha_justa = round(player["rpg"] * 0.92, 1)
        alertas.append(f"🙌 *{player['nome']} Over {linha_justa - 0.5} Rebotes*\n Projeção: {player['rpg']} reb | {player['min']} min\n Linha justa: {linha_justa}")
    if player["apg"] >= 6 and player["min"] >= 30:
        linha_justa = round(player["apg"] * 0.9, 1)
        alertas.append(f"🎯 *{player['nome']} Over {linha_justa - 0.5} Assist*\n Projeção: {player['apg']} ast | {player['min']} min\n Linha justa: {linha_justa}")
    if player["3pm"] >= 2.5 and player["min"] >= 28:
        linha_justa = round(player["3pm"] * 0.9, 1)
        alertas.append(f"🔥 *{player['nome']} Over {linha_justa - 0.5} Bolas 3*\n Projeção: {player['3pm']} 3PM | {player['min']} min\n Linha justa: {linha_justa}")
    return alertas

def formatar_alerta_basket(event, nome_liga, antecipado=False):
    stats = analisar_confronto_espn(event, nome_liga)
    liga = event["leagues"][0]["name"]
    casa = stats["casa_nome"]
    fora = stats["fora_nome"]

    data_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    hora_brt = data_utc - timedelta(hours=3)
    hora_brt_str = hora_brt.strftime("%H:%M")
    data_str = hora_brt.strftime("%d/%m")

    total_ppg = stats["total_ppg"]
    total_rpg = stats["total_rpg"]
    total_3pm = stats["total_3pm"]

    if total_ppg == 0:
        return None

    alertas = []
    if total_ppg >= 215:
        linha_justa = round(total_ppg * 0.98, 1)
        alertas.append(f"🏀 *Over Pontos*\n Projeção: {total_ppg} | Linha justa: {linha_justa}\n Buscar: Over {linha_justa - 2} @1.90+")
    if total_rpg >= 85:
        linha_justa = round(total_rpg * 0.95, 1)
        alertas.append(f"🙌 *Over Rebotes*\n Projeção: {total_rpg} | Linha justa: {linha_justa}")
    if total_3pm >= 25:
        linha_justa = round(total_3pm * 0.95, 1)
        alertas.append(f"🎯 *Over 3PT*\n Projeção: {total_3pm} | Linha justa: {linha_justa}")

    for p in stats["players_casa"][:3]:
        alertas.extend(analisar_props_jogador(p))
    for p in stats["players_fora"][:3]:
        alertas.extend(analisar_props_jogador(p))

    if not alertas:
        return None

    nivel = "🚨🔥 JOGO EXPLOSIVO" if total_ppg >= 230 else "🚨 JOGO QUENTE"
    if antecipado:
        nivel = "⏰🔥 ANTECIPADO " + nivel

    msg = (
        f"{nivel}\n{'━' * 28}\n🏆 {liga}\n🏀 *{casa} x {fora}*\n"
        f"🕐 {data_str} às {hora_brt_str} BRT\n\n"
        f"📊 *Projeção do Jogo:*\n"
        f"• Pontos: {total_ppg} ({stats['casa_ppg']}/{stats['fora_ppg']})\n"
        f"• Rebotes: {total_rpg}\n"
        f"• Assistências: {stats['total_apg']}\n"
        f"• Bolas 3: {total_3pm}\n\n"
        + "\n\n".join(alertas)
    )
    return msg

# ─── COMANDOS V20 ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Basket Alert Bot v2.3 - ESPN*\n\n"
        "🏀 NBA • WNBA • NCAAB\n"
        "📊 Pontos, Reb, Ast, 3PT\n"
        "⭐ Props de Jogadores\n"
        "⏰ Antecipados 3 dias\n"
        "🎯 100% Grátis • Sem limite\n\n"
        "/ping — Testar\n/hoje — Jogos hoje\n/amanha — Jogos amanhã\n/antecipados — 3 dias\n/alerta — Entradas quentes",
        parse_mode="Markdown"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Basket Bot v2.3 online! ESPN + Props OK")

async def hoje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Buscando jogos de hoje...")
    hoje_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        games = buscar_jogos_data(hoje_str, info["slug"])
        if not games: continue
        encontrou = True
        msg = f"🏀 *{nome_liga}:*\n"
        for g in games[:8]:
            comp = g["competitions"][0]
            casa = comp["competitors"][0]["team"]["displayName"]
            fora = comp["competitors"][1]["team"]["displayName"]
            hora_utc = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
            hora_brt = hora_utc - timedelta(hours=3)
            msg += f"🕐 {hora_brt.strftime('%H:%M')} — {casa} x {fora}\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        await update.message.reply_text("📭 Nenhum jogo hoje.")

async def amanha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 Analisando amanhã...")
    amanha_str = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        games = buscar_jogos_data(amanha_str, info["slug"])
        for g in games[:4]:
            msg = formatar_alerta_basket(g, nome_liga)
            if msg:
                encontrou = True
                await update.message.reply_text(msg, parse_mode="Markdown")
                time.sleep(2)
    if not encontrou:
        await update.message.reply_text("😴 Nenhuma entrada quente amanhã.")

async def antecipados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏰ Buscando odds desreguladas nos próximos 3 dias...")
    hoje = datetime.now(timezone.utc).date()
    encontrou = False
    total_alertas = 0
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            games = buscar_jogos_data(data_str, info["slug"])
            for g in games[:3]:
                msg = formatar_alerta_basket(g, nome_liga, antecipado=True)
                if msg:
                    encontrou = True
                    total_alertas += 1
                    await update.message.reply_text(msg, parse_mode="Markdown")
                    time.sleep(2)
    if not encontrou:
        await update.message.reply_text("😴 Nenhuma odd desregulada nos próximos 3 dias.")
    else:
        await update.message.reply_text(f"✅ {total_alertas} oportunidades únicas encontradas.\n\n💡 Casas abrem linha 2-3 dias antes. Pega agora antes de ajustarem!")

async def alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 Buscando entradas quentes hoje...")
    hoje_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    quente = False
    for nome_liga, info in LIGAS.items():
        games = buscar_jogos_data(hoje_str, info["slug"])
        for g in games:
            msg = formatar_alerta_basket(g, nome_liga)
            if msg:
                quente = True
                await update.message.reply_text(msg, parse_mode="Markdown")
                time.sleep(2)
    if not quente:
        await update.message.reply_text("😴 Nenhuma entrada quente hoje.")

# ─── FLASK + WEBHOOK V20 CORRIGIDO ────────────────────────────────────────────
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("ping", ping))
application.add_handler(CommandHandler("hoje", hoje))
application.add_handler(CommandHandler("amanha", amanha))
application.add_handler(CommandHandler("antecipados", antecipados))
application.add_handler(CommandHandler("alerta", alerta))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update)) # CORRIGIDO
    return 'ok'

@app.route('/')
def index():
    return 'Basket Bot v2.3 ESPN Online'

@app.route('/health')
def health():
    return 'ok', 200

def setup():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        loop.run_until_complete(application.bot.set_webhook(url=f"{RAILWAY_URL}/{TOKEN}"))
        logging.info("Webhook setado")

if __name__ == "__main__":
    setup()
    PORT = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=PORT)
