import os
import logging
import requests
from datetime import date, timedelta, time as dtime
from telegram.ext import Updater, CommandHandler
from telegram import Bot

TOKEN = os.environ.get("BOT_TOKEN")
API_KEY = os.environ.get("API_KEY")
CHAT_ID = os.environ.get("CHAT_ID")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

HEADERS = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
}

LIGAS = {
    "Brasileirao": {"id": "71", "season": "2025"},
    "Copa do Brasil": {"id": "73", "season": "2025"},
    "Libertadores": {"id": "13", "season": "2025"},
}

# ─── CACHE SIMPLES ────────────────────────────────────────────────────────────
_cache = {}

def cache_get(key):
    return _cache.get(key)

def cache_set(key, value):
    _cache[key] = value

# ─── FUNÇÕES DE BUSCA ─────────────────────────────────────────────────────────

def buscar_jogos_data(data_str, league_id, season):
    key = f"jogos_{data_str}_{league_id}_{season}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"date": data_str, "league": league_id, "season": season}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        result = resp.json().get("response", [])
    except Exception:
        result = []
    cache_set(key, result)
    return result

def buscar_stats(team_id, league_id, season):
    key = f"stats_{team_id}_{league_id}_{season}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = "https://api-football-v1.p.rapidapi.com/v3/teams/statistics"
    params = {"team": team_id, "league": league_id, "season": season}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        data = resp.json().get("response", {})
    except Exception:
        data = {}
    shots = data.get("shots", {})
    jogos = data.get("fixtures", {}).get("played", {}).get("total", 1) or 1
    total = shots.get("total", {}).get("total", 0) or 0
    no_alvo = shots.get("on", {}).get("total", 0) or 0
    gols = data.get("goals", {}).get("for", {}).get("total", {}).get("total", 0) or 0
    media = round(total / jogos, 1)
    media_alvo = round(no_alvo / jogos, 1)
    ofensividade = min(round((media / 20) * 100), 100)
    result = {
        "media": media,
        "media_alvo": media_alvo,
        "ofensividade": ofensividade,
        "jogos": jogos,
        "gols": gols,
    }
    cache_set(key, result)
    return result

def buscar_artilheiros(team_id, league_id, season):
    key = f"artilheiros_{team_id}_{league_id}_{season}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = "https://api-football-v1.p.rapidapi.com/v3/players"
    params = {"team": team_id, "league": league_id, "season": season, "page": "1"}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        players = resp.json().get("response", [])
    except Exception:
        players = []
    finalizadores = []
    for p in players:
        stats = p.get("statistics", [{}])[0]
        shots = stats.get("shots", {})
        total_shots = shots.get("total") or 0
        on_target = shots.get("on") or 0
        goals = stats.get("goals", {}).get("total") or 0
        name = p.get("player", {}).get("name", "")
        if total_shots >= 5:
            finalizadores.append({
                "nome": name,
                "chutes": total_shots,
                "no_alvo": on_target,
                "gols": goals,
            })
    finalizadores.sort(key=lambda x: x["chutes"], reverse=True)
    result = finalizadores[:3]
    cache_set(key, result)
    return result

# ─── FUNÇÕES DE ANÁLISE ───────────────────────────────────────────────────────

def calcular_confianca(total_media, importancia, ofens_casa, ofens_fora):
    base = min(total_media / 30 * 60, 60)
    bonus_imp = importancia * 2
    bonus_ofens = ((ofens_casa + ofens_fora) / 200) * 10
    return min(round(base + bonus_imp + bonus_ofens), 99)

def calcular_importancia(fixture):
    rodada = fixture.get("league", {}).get("round", "")
    liga_id = str(fixture.get("league", {}).get("id", ""))
    if "Final" in rodada:
        return 10
    if "Semi" in rodada:
        return 9
    if "Quarter" in rodada or "Quartas" in rodada:
        return 8
    if liga_id == "73":
        return 8
    if liga_id == "13":
        return 9
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
        stats_casa["ofensividade"], stats_fora["ofensividade"]
    )

    if dias_restantes == 0:
        prefixo = "🚨 *ALERTA HOJE*"
        janela = ""
    elif dias_restantes == 1:
        prefixo = "⚡ *ALERTA — AMANHÃ*"
        janela = "\n⏰ *Entrar hoje garante odd melhor!*"
    elif dias_restantes == 2:
        prefixo = "📅 *ANTECIPADO — 2 DIAS*"
        janela = "\n⏰ *Odd ainda desregulada — janela aberta!*"
    else:
        prefixo = "📅 *ANTECIPADO — 3 DIAS*"
        janela = "\n⏰ *Melhor momento para entrar — odd no pico!*"

    mercado_linha = round(total_media - 3, 1)

    fin_casa_txt = ""
    for f in fin_casa:
        fin_casa_txt += f"  ⚡ {f['nome']}: {f['chutes']} chutes | {f['no_alvo']} no alvo | {f['gols']} gols\n"
    if not fin_casa_txt:
        fin_casa_txt = "  Dados indisponíveis\n"

    fin_fora_txt = ""
    for f in fin_fora:
        fin_fora_txt += f"  ⚡ {f['nome']}: {f['chutes']} chutes | {f['no_alvo']} no alvo | {f['gols']} gols\n"
    if not fin_fora_txt:
        fin_fora_txt = "  Dados indisponíveis\n"

    msg = (
        f"{prefixo}\n"
        f"{'━' * 28}\n"
        f"🏆 {liga}\n"
        f"⚽ *{casa} x {fora}*\n"
        f"🕐 {data_jogo} às {hora}\n\n"
        f"📊 *Finalizações por jogo:*\n"
        f"🏠 {casa}: *{stats_casa['media']}* fin | {stats_casa['media_alvo']} no alvo\n"
        f"✈️ {fora}: *{stats_fora['media']}* fin | {stats_fora['media_alvo']} no alvo\n"
        f"📈 Total combinado: *{total_media} fin/jogo*\n\n"
        f"🎯 *Principais finalizadores:*\n"
        f"🏠 {casa}:\n{fin_casa_txt}"
        f"✈️ {fora}:\n{fin_fora_txt}\n"
        f"💪 *Ofensividade:*\n"
        f"🏠 {casa}: {stats_casa['ofensividade']}%\n"
        f"✈️ {fora}: {stats_fora['ofensividade']}%\n\n"
        f"🔥 Importância: {importancia}/10\n"
        f"✅ *Confiança: {confianca}%*\n\n"
        f"💰 *Mercado sugerido:*\n"
        f"Ambos Over {mercado_linha} finalizações\n\n"
        f"🏦 *Verificar odds em:*\n"
        f"• Superbet\n"
        f"• Bet365\n"
        f"• Betano\n"
        f"• KTO\n"
        f"• Novabet"
        f"{janela}"
    )
    return msg, confianca

def analisar_jogos(fixtures, league_id, season, dias_restantes=0, limite=5, min_confianca=60):
    alertas = []
    for f in fixtures[:limite]:
        id_casa = f["teams"]["home"]["id"]
        id_fora = f["teams"]["away"]["id"]
        stats_casa = buscar_stats(id_casa, league_id, season)
        stats_fora = buscar_stats(id_fora, league_id, season)
        total = stats_casa["media"] + stats_fora["media"]
        if total >= 15:
            fin_casa = buscar_artilheiros(id_casa, league_id, season)
            fin_fora = buscar_artilheiros(id_fora, league_id, season)
            msg, confianca = formatar_alerta(
                f, stats_casa, stats_fora,
                fin_casa, fin_fora, dias_restantes
            )
            if confianca >= min_confianca:
                alertas.append((confianca, msg))
    alertas.sort(reverse=True)
    return alertas

# ─── ALERTA AUTOMÁTICO ────────────────────────────────────────────────────────

def alerta_automatico(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    hoje = date.today()
    bot.send_message(
        chat_id=CHAT_ID,
        text="🌅 *Bom dia! Analisando jogos de hoje e próximos dias...*",
        parse_mode="Markdown"
    )
    encontrou = False
    for dias in range(4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            fixtures = buscar_jogos_data(data_str, info["id"], info["season"])
            if not fixtures:
                continue
            alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=dias)
            for confianca, msg in alertas:
                encontrou = True
                bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    if not encontrou:
        bot.send_message(
            chat_id=CHAT_ID,
            text="📭 Nenhuma oportunidade forte encontrada hoje.",
            parse_mode="Markdown"
        )

# ─── COMANDOS ─────────────────────────────────────────────────────────────────

def start(update, context):
    update.message.reply_text(
        "🤖 *Finalizações Bot v2.0*\n\n"
        "Comandos:\n"
        "/jogos — Jogos de hoje\n"
        "/alerta — Alertas com finalizadores\n"
        "/antecipados — Próximos 3 dias\n"
        "/setid — Ativar alertas às 08h\n"
        "/ping — Testar bot",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v2.0 online!")

def setid(update, context):
    chat_id = str(update.message.chat_id)
    update.message.reply_text(
        f"✅ Seu Chat ID: `{chat_id}`\n\n"
        f"Adicione no Railway → Variables:\n"
        f"Key: `CHAT_ID`\n"
        f"Value: `{chat_id}`\n\n"
        f"Alertas automáticos todo dia às 08h! ✅",
        parse_mode="Markdown"
    )

def jogos(update, context):
    update.message.reply_text("🔍 Buscando jogos de hoje...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        if not fixtures:
            continue
        encontrou = True
        msg = f"⚽ *{nome_liga} — Hoje:*\n\n"
        for f in fixtures[:5]:
            casa = f["teams"]["home"]["name"]
            fora = f["teams"]["away"]["name"]
            hora = f["fixture"]["date"][11:16]
            msg += f"🕐 {hora} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhum jogo hoje nas ligas monitoradas.")

def alerta(update, context):
    update.message.reply_text("📊 Analisando finalizações de hoje...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        if not fixtures:
            continue
        alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=0)
        for confianca, msg in alertas:
            encontrou = True
            update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("⚠️ Nenhum jogo com média alta hoje.")

def antecipados(update, context):
    update.message.reply_text("📅 Buscando oportunidades nos próximos 3 dias...")
    hoje = date.today()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            fixtures = buscar_jogos_data(data_str, info["id"], info["season"])
            if not fixtures:
                continue
            alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=dias)
            for confianca, msg in alertas:
                encontrou = True
                update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhuma oportunidade antecipada encontrada.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("jogos", jogos))
    dp.add_handler(CommandHandler("alerta", alerta))
    dp.add_handler(CommandHandler("antecipados", antecipados))
    dp.add_handler(CommandHandler("setid", setid))

    updater.job_queue.run_daily(
        alerta_automatico,
        time=dtime(hour=11, minute=0)
    )

    print("Bot v2.0 rodando...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
