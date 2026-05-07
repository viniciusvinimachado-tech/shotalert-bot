import os
import json
import logging
import requests
from datetime import date, timedelta, time as dtime, datetime
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

# ─── LIGAS ────────────────────────────────────────────────────────────────────
LIGAS = {
    "Brasileirao":        {"id": "71",  "season": "2025"},
    "Copa do Brasil":     {"id": "73",  "season": "2025"},
    "Libertadores":       {"id": "13",  "season": "2025"},
    "Sul-Americana":      {"id": "11",  "season": "2025"},
    "Champions League":   {"id": "2",   "season": "2024"},
    "Europa League":      {"id": "3",   "season": "2024"},
    "Conference League":  {"id": "848", "season": "2024"},
    "Premier League":     {"id": "39",  "season": "2024"},
    "La Liga":            {"id": "140", "season": "2024"},
    "Serie A Italia":     {"id": "135", "season": "2024"},
    "Bundesliga":         {"id": "78",  "season": "2024"},
    "Ligue 1":            {"id": "61",  "season": "2024"},
    "Eredivisie":         {"id": "88",  "season": "2024"},
    "Liga Portugal":      {"id": "94",  "season": "2024"},
    "MLS":                {"id": "253", "season": "2025"},
    "Liga MX":            {"id": "262", "season": "2025"},
    "Argentina Liga":     {"id": "128", "season": "2024"},
}

# ─── BANCO DE DADOS LOCAL (arquivo JSON) ──────────────────────────────────────
DB_FILE = "historico.json"

def carregar_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"alertas": [], "mensal": {}}

def salvar_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

def registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, mercado, linha, confianca):
    db = carregar_db()
    db["alertas"].append({
        "fixture_id": fixture_id,
        "casa": casa,
        "fora": fora,
        "liga": liga,
        "data": data_jogo,
        "hora": hora,
        "mercado": mercado,
        "linha": linha,
        "confianca": confianca,
        "resultado": None,  # None = pendente, True = acerto, False = erro
        "fin_real": None,   # finalizações reais do jogo
        "registrado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    salvar_db(db)

def atualizar_resultado(fixture_id, acerto, fin_real):
    db = carregar_db()
    for alerta in db["alertas"]:
        if alerta["fixture_id"] == fixture_id and alerta["resultado"] is None:
            alerta["resultado"] = acerto
            alerta["fin_real"] = fin_real
            break
    salvar_db(db)

# ─── CACHE ────────────────────────────────────────────────────────────────────
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
    media = round(total / jogos, 1)
    media_alvo = round(no_alvo / jogos, 1)
    ofensividade = min(round((media / 20) * 100), 100)
    result = {"media": media, "media_alvo": media_alvo, "ofensividade": ofensividade, "jogos": jogos}
    cache_set(key, result)
    return result

def buscar_resultado_jogo(fixture_id):
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"id": fixture_id}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        data = resp.json().get("response", [])
        if not data:
            return None, None
        jogo = data[0]
        status = jogo.get("fixture", {}).get("status", {}).get("short", "")
        if status not in ["FT", "AET", "PEN"]:
            return None, None  # Jogo não terminou
        stats = jogo.get("statistics", [])
        fin_casa = 0
        fin_fora = 0
        for s in stats:
            for item in s.get("statistics", []):
                if item.get("type") == "Total Shots":
                    val = item.get("value") or 0
                    if s.get("team", {}).get("id") == jogo["teams"]["home"]["id"]:
                        fin_casa = int(val)
                    else:
                        fin_fora = int(val)
        return fin_casa + fin_fora, status
    except Exception:
        return None, None

# ─── ANÁLISE ──────────────────────────────────────────────────────────────────

def calcular_confianca(total_media, importancia, ofens_casa, ofens_fora, jogos_casa, jogos_fora):
    # Base pela média de finalizações
    base = min(total_media / 35 * 55, 55)
    # Bonus por importância
    bonus_imp = importancia * 2
    # Bonus por ofensividade
    bonus_ofens = ((ofens_casa + ofens_fora) / 200) * 8
    # Bonus por amostra (mais jogos = mais confiável)
    bonus_amostra = min(((jogos_casa + jogos_fora) / 2) / 30 * 5, 5)
    total = base + bonus_imp + bonus_ofens + bonus_amostra
    return min(round(total), 99)

def calcular_importancia(fixture):
    rodada = fixture.get("league", {}).get("round", "")
    liga_id = str(fixture.get("league", {}).get("id", ""))
    if "Final" in rodada:
        return 10
    if "Semi" in rodada:
        return 9
    if "Quarter" in rodada:
        return 8
    if liga_id in ["2", "13"]:
        return 9
    if liga_id in ["3", "11", "73", "848"]:
        return 8
    return 7

def formatar_alerta(fixture, stats_casa, stats_fora, dias_restantes=0):
    casa = fixture["teams"]["home"]["name"]
    fora = fixture["teams"]["away"]["name"]
    fixture_id = fixture["fixture"]["id"]
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

    if dias_restantes == 0:
        prefixo = "🚨 *ALERTA HOJE*"
        janela = ""
    elif dias_restantes == 1:
        prefixo = "⚡ *ALERTA — AMANHÃ*"
        janela = "\n⏰ *Entrar hoje garante odd melhor!*"
    elif dias_restantes == 2:
        prefixo = "📅 *ANTECIPADO — 2 DIAS*"
        janela = "\n⏰ *Odd desregulada — janela aberta!*"
    else:
        prefixo = "📅 *ANTECIPADO — 3 DIAS*"
        janela = "\n⏰ *Melhor momento — odd no pico!*"

    linha = round(total_media - 3, 1)

    msg = (
        f"{prefixo}\n"
        f"{'━' * 28}\n"
        f"🏆 {liga}\n"
        f"⚽ *{casa} x {fora}*\n"
        f"🕐 {data_jogo} às {hora}\n\n"
        f"📊 *Análise de Finalizações:*\n"
        f"🏠 {casa}: *{stats_casa['media']}* fin/jogo | {stats_casa['media_alvo']} no alvo\n"
        f"✈️ {fora}: *{stats_fora['media']}* fin/jogo | {stats_fora['media_alvo']} no alvo\n"
        f"📈 Combinado: *{total_media} fin/jogo*\n\n"
        f"💪 Ofensividade:\n"
        f"🏠 {stats_casa['ofensividade']}% | ✈️ {stats_fora['ofensividade']}%\n\n"
        f"🔥 Importância: {importancia}/10\n"
        f"✅ *Confiança: {confianca}%*\n\n"
        f"💰 *Mercado: Over {linha} finalizações totais*\n\n"
        f"🏦 Verificar odds:\n"
        f"• Superbet • Bet365\n"
        f"• Betano • KTO • Novabet"
        f"{janela}"
    )
    return msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora

def analisar_jogos(fixtures, league_id, season, dias_restantes=0, limite=3):
    alertas = []
    for f in fixtures[:limite]:
        id_casa = f["teams"]["home"]["id"]
        id_fora = f["teams"]["away"]["id"]
        stats_casa = buscar_stats(id_casa, league_id, season)
        stats_fora = buscar_stats(id_fora, league_id, season)
        total = stats_casa["media"] + stats_fora["media"]
        if total >= 18:
            resultado = formatar_alerta(f, stats_casa, stats_fora, dias_restantes)
            msg, confianca = resultado[0], resultado[1]
            if confianca >= 80:  # Filtro rigoroso — só acima de 80%
                alertas.append(resultado)
    alertas.sort(key=lambda x: x[1], reverse=True)
    return alertas[:3]  # Máximo 3 alertas por dia

# ─── VERIFICAR RESULTADOS ─────────────────────────────────────────────────────

def verificar_resultados(context):
    db = carregar_db()
    pendentes = [a for a in db["alertas"] if a["resultado"] is None]
    if not pendentes:
        return
    for alerta in pendentes:
        fin_real, status = buscar_resultado_jogo(alerta["fixture_id"])
        if fin_real is None:
            continue
        acerto = fin_real > alerta["linha"]
        atualizar_resultado(alerta["fixture_id"], acerto, fin_real)

# ─── RELATÓRIO DIÁRIO ─────────────────────────────────────────────────────────

def relatorio_diario(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    hoje = date.today().strftime("%Y-%m-%d")
    db = carregar_db()
    alertas_hoje = [a for a in db["alertas"] if a["data"] == hoje and a["resultado"] is not None]

    if not alertas_hoje:
        bot.send_message(
            chat_id=CHAT_ID,
            text="📋 *Relatório de hoje:*\n\nNenhuma entrada finalizada hoje.",
            parse_mode="Markdown"
        )
        return

    acertos = [a for a in alertas_hoje if a["resultado"] is True]
    erros = [a for a in alertas_hoje if a["resultado"] is False]
    pct = round(len(acertos) / len(alertas_hoje) * 100)

    msg = f"📋 *Relatório Diário — {hoje}*\n{'━' * 28}\n\n"
    msg += f"✅ Acertos: {len(acertos)}\n"
    msg += f"❌ Erros: {len(erros)}\n"
    msg += f"🎯 Taxa: *{pct}%*\n\n"

    if acertos:
        msg += "✅ *Entradas certas:*\n"
        for a in acertos:
            msg += f"• {a['casa']} x {a['fora']} — {a['fin_real']} fin (linha {a['linha']})\n"

    if erros:
        msg += "\n❌ *Entradas erradas:*\n"
        for a in erros:
            msg += f"• {a['casa']} x {a['fora']} — {a['fin_real']} fin (linha {a['linha']})\n"

    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ─── RELATÓRIO MENSAL ─────────────────────────────────────────────────────────

def relatorio_mensal(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    hoje = date.today()
    mes = hoje.strftime("%Y-%m")
    db = carregar_db()
    alertas_mes = [a for a in db["alertas"] if a["data"].startswith(mes) and a["resultado"] is not None]

    if not alertas_mes:
        bot.send_message(chat_id=CHAT_ID, text="📅 Sem dados suficientes este mês.", parse_mode="Markdown")
        return

    acertos = [a for a in alertas_mes if a["resultado"] is True]
    total = len(alertas_mes)
    pct = round(len(acertos) / total * 100)

    # Simulação de banca R$500
    banca = 500.0
    ganho_por_acerto = banca * 0.05   # 5% da banca por entrada
    perda_por_erro = banca * 0.03     # 3% da banca por erro
    lucro = (len(acertos) * ganho_por_acerto) - ((total - len(acertos)) * perda_por_erro)
    banca_final = banca + lucro
    roi = round((lucro / banca) * 100, 1)

    msg = (
        f"📅 *Relatório Mensal — {mes}*\n"
        f"{'━' * 28}\n\n"
        f"📊 Total de entradas: {total}\n"
        f"✅ Acertos: {len(acertos)}\n"
        f"❌ Erros: {total - len(acertos)}\n"
        f"🎯 *Taxa de acerto: {pct}%*\n\n"
        f"💰 *Simulação Banca R$500:*\n"
        f"Banca inicial: R$ 500,00\n"
        f"Lucro/Prejuízo: R$ {lucro:+.2f}\n"
        f"Banca final: R$ {banca_final:.2f}\n"
        f"ROI: *{roi}%*\n\n"
        f"{'🟢 Mês lucrativo!' if lucro > 0 else '🔴 Mês negativo.'}"
    )
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ─── ALERTA AUTOMÁTICO ────────────────────────────────────────────────────────

def alerta_automatico(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    hoje = date.today()
    bot.send_message(
        chat_id=CHAT_ID,
        text="🌅 *Bom dia! Buscando as melhores entradas...*",
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
            for resultado in alertas:
                msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
                encontrou = True
                registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, "Over finalizações", linha, confianca)
                bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    if not encontrou:
        bot.send_message(
            chat_id=CHAT_ID,
            text="📭 Nenhuma entrada com 80%+ de confiança hoje.\nO bot só avisa quando vale a pena! 🎯",
            parse_mode="Markdown"
        )

# ─── COMANDOS ─────────────────────────────────────────────────────────────────

def start(update, context):
    update.message.reply_text(
        "🤖 *Finalizações Bot v4.0*\n\n"
        "🌍 18 ligas • 🎯 Filtro 80%+ • 📊 Histórico\n\n"
        "Comandos:\n"
        "/jogos — Jogos de hoje\n"
        "/alerta — Alertas de hoje\n"
        "/antecipados — Próximos 3 dias\n"
        "/historico — Ver histórico de acertos\n"
        "/relatorio — Relatório do mês\n"
        "/ligas — Ver ligas monitoradas\n"
        "/setid — Ativar alertas às 08h\n"
        "/ping — Testar bot",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v4.0 online! 🌍 18 ligas | 🎯 Filtro 80%+")

def setid(update, context):
    chat_id = str(update.message.chat_id)
    update.message.reply_text(
        f"✅ Chat ID: `{chat_id}`\n\nRailway → Variables:\nKey: `CHAT_ID`\nValue: `{chat_id}`",
        parse_mode="Markdown"
    )

def ligas(update, context):
    msg = (
        "🌍 *18 Ligas monitoradas:*\n\n"
        "🇧🇷 Brasileirão • Copa do Brasil\n"
        "Libertadores • Sul-Americana\n\n"
        "🇪🇺 Champions • Europa League\n"
        "Conference • Premier League\n"
        "La Liga • Serie A • Bundesliga\n"
        "Ligue 1 • Eredivisie • Liga Portugal\n\n"
        "🌎 MLS • Liga MX • Argentina"
    )
    update.message.reply_text(msg, parse_mode="Markdown")

def historico(update, context):
    db = carregar_db()
    finalizados = [a for a in db["alertas"] if a["resultado"] is not None]
    pendentes = [a for a in db["alertas"] if a["resultado"] is None]

    if not finalizados:
        update.message.reply_text("📊 Ainda sem histórico de resultados.\nAguarde os primeiros jogos serem finalizados!")
        return

    acertos = [a for a in finalizados if a["resultado"] is True]
    pct = round(len(acertos) / len(finalizados) * 100)

    msg = (
        f"📊 *Histórico Geral*\n"
        f"{'━' * 28}\n\n"
        f"Total enviados: {len(db['alertas'])}\n"
        f"✅ Acertos: {len(acertos)}\n"
        f"❌ Erros: {len(finalizados) - len(acertos)}\n"
        f"⏳ Pendentes: {len(pendentes)}\n"
        f"🎯 *Taxa: {pct}%*\n\n"
        f"*Últimas 5 entradas:*\n"
    )
    for a in finalizados[-5:]:
        icon = "✅" if a["resultado"] else "❌"
        msg += f"{icon} {a['casa']} x {a['fora']} — {a['fin_real']} fin\n"

    update.message.reply_text(msg, parse_mode="Markdown")

def relatorio(update, context):
    hoje = date.today()
    mes = hoje.strftime("%Y-%m")
    db = carregar_db()
    alertas_mes = [a for a in db["alertas"] if a["data"].startswith(mes) and a["resultado"] is not None]

    if not alertas_mes:
        update.message.reply_text("📅 Sem resultados finalizados este mês ainda.")
        return

    acertos = [a for a in alertas_mes if a["resultado"] is True]
    total = len(alertas_mes)
    pct = round(len(acertos) / total * 100)
    banca = 500.0
    ganho = len(acertos) * banca * 0.05
    perda = (total - len(acertos)) * banca * 0.03
    lucro = ganho - perda
    banca_final = banca + lucro
    roi = round((lucro / banca) * 100, 1)

    msg = (
        f"📅 *Relatório — {mes}*\n"
        f"{'━' * 28}\n\n"
        f"Entradas: {total} | ✅ {len(acertos)} | ❌ {total - len(acertos)}\n"
        f"🎯 *Taxa: {pct}%*\n\n"
        f"💰 *Banca R$500:*\n"
        f"Lucro: R$ {lucro:+.2f}\n"
        f"Banca final: R$ {banca_final:.2f}\n"
        f"ROI: *{roi}%*\n\n"
        f"{'🟢 Mês lucrativo!' if lucro > 0 else '🔴 Mês negativo.'}"
    )
    update.message.reply_text(msg, parse_mode="Markdown")

def jogos(update, context):
    update.message.reply_text("🔍 Buscando jogos de hoje...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        if not fixtures:
            continue
        encontrou = True
        msg = f"⚽ *{nome_liga}:*\n"
        for f in fixtures[:3]:
            casa = f["teams"]["home"]["name"]
            fora = f["teams"]["away"]["name"]
            hora = f["fixture"]["date"][11:16]
            msg += f"🕐 {hora} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhum jogo hoje nas 18 ligas.")

def alerta(update, context):
    update.message.reply_text("📊 Analisando e filtrando melhores entradas...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        if not fixtures:
            continue
        alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=0)
        for resultado in alertas:
            msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
            encontrou = True
            registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, "Over finalizações", linha, confianca)
            update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text(
            "🎯 Nenhuma entrada com 80%+ hoje.\nO bot só avisa quando realmente vale!"
        )

def antecipados(update, context):
    update.message.reply_text("📅 Buscando oportunidades antecipadas...")
    hoje = date.today()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            fixtures = buscar_jogos_data(data_str, info["id"], info["season"])
            if not fixtures:
                continue
            alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=dias)
            for resultado in alertas:
                msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
                encontrou = True
                registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, "Over finalizações", linha, confianca)
                update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhuma oportunidade antecipada com 80%+.")

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
    dp.add_handler(CommandHandler("ligas", ligas))
    dp.add_handler(CommandHandler("historico", historico))
    dp.add_handler(CommandHandler("relatorio", relatorio))

    jq = updater.job_queue

    # Alerta automático às 08h BRT (11h UTC)
    jq.run_daily(alerta_automatico, time=dtime(hour=11, minute=0))

    # Verificar resultados a cada 2h
    jq.run_repeating(verificar_resultados, interval=7200, first=60)

    # Relatório diário às 23h BRT (02h UTC)
    jq.run_daily(relatorio_diario, time=dtime(hour=2, minute=0))

    # Relatório mensal todo dia 1 às 08h
    jq.run_daily(relatorio_mensal, time=dtime(hour=11, minute=30))

    print("Bot v4.0 rodando — 18 ligas | Histórico ativo!")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
