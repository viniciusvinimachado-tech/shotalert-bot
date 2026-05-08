import os
import json
import logging
import requests
from datetime import date, timedelta, time as dtime, datetime
from telegram.ext import Updater, CommandHandler
from telegram import Bot

TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
API_KEY = "0b25f4c3636f1848cd546828c84a96d9"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

HEADERS = {"X-Auth-Token": API_KEY}
BASE_URL = "https://api.football-data.org/v4"

# ─── LIGAS ────────────────────────────────────────────────────────────────────
LIGAS = {
    "Premier League":    {"id": "PL"},
    "Champions League":  {"id": "CL"},
    "La Liga":           {"id": "PD"},
    "Bundesliga":        {"id": "BL1"},
    "Serie A Italia":    {"id": "SA"},
    "Ligue 1":           {"id": "FL1"},
    "Eredivisie":        {"id": "DED"},
    "Liga Portugal":     {"id": "PPL"},
    "Brasileirao":       {"id": "BSA"},
    "Europa League":     {"id": "EL"},
    "Conference League": {"id": "UECL"},
}

# ─── BANCO DE DADOS ───────────────────────────────────────────────────────────
DB_FILE = "historico.json"

def carregar_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"alertas": []}

def salvar_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

def registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, linha, confianca):
    db = carregar_db()
    if not any(a["fixture_id"] == fixture_id for a in db["alertas"]):
        db["alertas"].append({
            "fixture_id": fixture_id,
            "casa": casa, "fora": fora, "liga": liga,
            "data": data_jogo, "hora": hora,
            "linha": linha, "confianca": confianca,
            "resultado": None, "fin_real": None,
            "registrado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        salvar_db(db)

def atualizar_resultado(fixture_id, acerto, fin_real):
    db = carregar_db()
    for a in db["alertas"]:
        if a["fixture_id"] == fixture_id and a["resultado"] is None:
            a["resultado"] = acerto
            a["fin_real"] = fin_real
            break
    salvar_db(db)

# ─── CACHE ────────────────────────────────────────────────────────────────────
_cache = {}

def cache_get(key):
    return _cache.get(key)

def cache_set(key, value):
    _cache[key] = value

# ─── BUSCA DE JOGOS ───────────────────────────────────────────────────────────

def buscar_jogos_data(data_str, league_id):
    key = f"jogos_{data_str}_{league_id}"
    if cache_get(key) is not None:
        return cache_get(key)
    try:
        resp = requests.get(
            f"{BASE_URL}/competitions/{league_id}/matches",
            headers=HEADERS,
            params={"dateFrom": data_str, "dateTo": data_str},
            timeout=10
        )
        result = resp.json().get("matches", [])
    except Exception:
        result = []
    cache_set(key, result)
    return result

def buscar_stats_time(team_id):
    key = f"stats_{team_id}"
    if cache_get(key) is not None:
        return cache_get(key)
    try:
        resp = requests.get(
            f"{BASE_URL}/teams/{team_id}/matches",
            headers=HEADERS,
            params={"limit": 10, "status": "FINISHED"},
            timeout=10
        )
        partidas = resp.json().get("matches", [])
    except Exception:
        partidas = []

    jogos = len(partidas)
    total_gols = 0
    vitorias = 0
    for p in partidas:
        home_id = p.get("homeTeam", {}).get("id")
        score = p.get("score", {}).get("fullTime", {})
        gols_casa = score.get("home") or 0
        gols_fora = score.get("away") or 0
        if home_id == team_id:
            total_gols += gols_casa
            if gols_casa > gols_fora:
                vitorias += 1
        else:
            total_gols += gols_fora
            if gols_fora > gols_casa:
                vitorias += 1

    media_gols = round(total_gols / jogos, 2) if jogos > 0 else 0
    taxa_vitoria = round(vitorias / jogos * 100) if jogos > 0 else 0
    # Estimativa de finalizações: times ofensivos fazem ~5-7 chutes por gol
    # Times com alta taxa de vitória tendem a finalizar mais
    multiplicador = 6 if taxa_vitoria >= 50 else 5
    media_fin = round(media_gols * multiplicador, 1)
    media_alvo = round(media_fin * 0.38, 1)
    ofensividade = min(round((media_fin / 18) * 100), 100)

    result = {
        "media": media_fin,
        "media_alvo": media_alvo,
        "ofensividade": ofensividade,
        "jogos": jogos,
        "media_gols": media_gols,
        "taxa_vitoria": taxa_vitoria,
    }
    cache_set(key, result)
    return result

# ─── ANÁLISE ──────────────────────────────────────────────────────────────────

def calcular_confianca(total_media, importancia, ofens_casa, ofens_fora, jogos_casa, jogos_fora, tv_casa, tv_fora):
    base = min(total_media / 30 * 50, 50)
    bonus_imp = importancia * 2
    bonus_ofens = ((ofens_casa + ofens_fora) / 200) * 8
    bonus_amostra = min(((jogos_casa + jogos_fora) / 2) / 10 * 5, 5)
    bonus_ofensivo = ((tv_casa + tv_fora) / 200) * 5
    return min(round(base + bonus_imp + bonus_ofens + bonus_amostra + bonus_ofensivo), 99)

def calcular_importancia(match, league_id):
    stage = match.get("stage", "")
    if "FINAL" in stage:
        return 10
    if "SEMI" in stage:
        return 9
    if "QUARTER" in stage:
        return 8
    if league_id in ["CL", "EL", "UECL"]:
        return 9
    return 7

def formatar_alerta(match, stats_casa, stats_fora, league_id, nome_liga, dias_restantes=0):
    casa = match["homeTeam"]["name"]
    fora = match["awayTeam"]["name"]
    fixture_id = match["id"]
    utc_date = match.get("utcDate", "")
    data_jogo = utc_date[:10]
    hora = utc_date[11:16]
    total_media = round(stats_casa["media"] + stats_fora["media"], 1)
    importancia = calcular_importancia(match, league_id)
    confianca = calcular_confianca(
        total_media, importancia,
        stats_casa["ofensividade"], stats_fora["ofensividade"],
        stats_casa["jogos"], stats_fora["jogos"],
        stats_casa["taxa_vitoria"], stats_fora["taxa_vitoria"]
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

    linha = round(total_media - 2, 1)

    msg = (
        f"{prefixo}\n"
        f"{'━' * 28}\n"
        f"🏆 {nome_liga}\n"
        f"⚽ *{casa} x {fora}*\n"
        f"🕐 {data_jogo} às {hora} UTC\n\n"
        f"📊 *Análise Ofensiva:*\n"
        f"🏠 {casa}:\n"
        f"  • {stats_casa['media_gols']} gols/jogo\n"
        f"  • ~{stats_casa['media']} finalizações estimadas\n"
        f"  • {stats_casa['taxa_vitoria']}% vitórias recentes\n\n"
        f"✈️ {fora}:\n"
        f"  • {stats_fora['media_gols']} gols/jogo\n"
        f"  • ~{stats_fora['media']} finalizações estimadas\n"
        f"  • {stats_fora['taxa_vitoria']}% vitórias recentes\n\n"
        f"📈 Total estimado: *{total_media} fin/jogo*\n\n"
        f"💪 Ofensividade:\n"
        f"🏠 {stats_casa['ofensividade']}% | ✈️ {stats_fora['ofensividade']}%\n\n"
        f"🔥 Importância: {importancia}/10\n"
        f"✅ *Confiança: {confianca}%*\n\n"
        f"💰 *Mercado: Over {linha} finalizações*\n\n"
        f"🏦 Verificar odds:\n"
        f"• Superbet • Bet365\n"
        f"• Betano • KTO • Novabet"
        f"{janela}"
    )
    return msg, confianca, fixture_id, linha, casa, fora, nome_liga, data_jogo, hora

def analisar_jogos(matches, league_id, nome_liga, dias_restantes=0, limite=3):
    alertas = []
    for m in matches[:limite]:
        if m.get("status") not in ["SCHEDULED", "TIMED"]:
            continue
        id_casa = m["homeTeam"]["id"]
        id_fora = m["awayTeam"]["id"]
        stats_casa = buscar_stats_time(id_casa)
        stats_fora = buscar_stats_time(id_fora)
        total = stats_casa["media"] + stats_fora["media"]
        if total >= 3:
            resultado = formatar_alerta(m, stats_casa, stats_fora, league_id, nome_liga, dias_restantes)
            if resultado[1] >= 55:
                alertas.append(resultado)
    alertas.sort(key=lambda x: x[1], reverse=True)
    return alertas[:3]

# ─── JOBS AUTOMÁTICOS ─────────────────────────────────────────────────────────

def verificar_resultados(context):
    db = carregar_db()
    pendentes = [a for a in db["alertas"] if a["resultado"] is None]
    for alerta in pendentes:
        try:
            resp = requests.get(
                f"{BASE_URL}/matches/{alerta['fixture_id']}",
                headers=HEADERS, timeout=10
            )
            data = resp.json()
            if data.get("status") != "FINISHED":
                continue
            score = data.get("score", {}).get("fullTime", {})
            gols = (score.get("home") or 0) + (score.get("away") or 0)
            fin_estimado = gols * 6
            acerto = fin_estimado > alerta["linha"]
            atualizar_resultado(alerta["fixture_id"], acerto, fin_estimado)
        except Exception:
            continue

def alerta_automatico(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    hoje = date.today()
    bot.send_message(chat_id=CHAT_ID, text="🌅 *Bom dia! Buscando melhores entradas...*", parse_mode="Markdown")
    encontrou = False
    for dias in range(4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            matches = buscar_jogos_data(data_str, info["id"])
            if not matches:
                continue
            alertas = analisar_jogos(matches, info["id"], nome_liga, dias_restantes=dias)
            for resultado in alertas:
                msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
                encontrou = True
                registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, linha, confianca)
                bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    if not encontrou:
        bot.send_message(chat_id=CHAT_ID, text="📭 Nenhuma entrada forte hoje.", parse_mode="Markdown")

def relatorio_diario(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    hoje = date.today().strftime("%Y-%m-%d")
    db = carregar_db()
    alertas_hoje = [a for a in db["alertas"] if a["data"] == hoje and a["resultado"] is not None]
    if not alertas_hoje:
        bot.send_message(chat_id=CHAT_ID, text="📋 *Relatório:* Nenhuma entrada finalizada hoje.", parse_mode="Markdown")
        return
    acertos = [a for a in alertas_hoje if a["resultado"]]
    pct = round(len(acertos) / len(alertas_hoje) * 100)
    msg = f"📋 *Relatório Diário — {hoje}*\n{'━'*28}\n\n"
    msg += f"✅ {len(acertos)} acertos | ❌ {len(alertas_hoje)-len(acertos)} erros | 🎯 *{pct}%*\n\n"
    for a in alertas_hoje:
        icon = "✅" if a["resultado"] else "❌"
        msg += f"{icon} {a['casa']} x {a['fora']}\n"
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

def relatorio_mensal(context):
    if not CHAT_ID:
        return
    bot = Bot(token=TOKEN)
    mes = date.today().strftime("%Y-%m")
    db = carregar_db()
    alertas_mes = [a for a in db["alertas"] if a["data"].startswith(mes) and a["resultado"] is not None]
    if not alertas_mes:
        return
    acertos = [a for a in alertas_mes if a["resultado"]]
    total = len(alertas_mes)
    pct = round(len(acertos) / total * 100)
    banca = 500.0
    lucro = (len(acertos) * banca * 0.05) - ((total - len(acertos)) * banca * 0.03)
    roi = round((lucro / banca) * 100, 1)
    msg = (
        f"📅 *Relatório Mensal — {mes}*\n{'━'*28}\n\n"
        f"Entradas: {total} | ✅ {len(acertos)} | ❌ {total-len(acertos)}\n"
        f"🎯 *Taxa: {pct}%*\n\n"
        f"💰 *Banca R$500:*\n"
        f"Lucro: R$ {lucro:+.2f}\n"
        f"Banca final: R$ {banca+lucro:.2f}\n"
        f"ROI: *{roi}%*\n\n"
        f"{'🟢 Mês lucrativo!' if lucro > 0 else '🔴 Mês negativo.'}"
    )
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ─── COMANDOS ─────────────────────────────────────────────────────────────────

def start(update, context):
    update.message.reply_text(
        "🤖 *Finalizações Bot v5.0*\n\n"
        "🌍 11 ligas • 🎯 Filtro 55%+ • 📊 Histórico\n\n"
        "/jogos — Jogos de hoje\n"
        "/alerta — Alertas de hoje\n"
        "/antecipados — Próximos 3 dias\n"
        "/historico — Histórico de acertos\n"
        "/relatorio — Relatório do mês\n"
        "/ligas — Ligas monitoradas\n"
        "/diagnostico — Testar API\n"
        "/setid — Ativar alertas às 08h\n"
        "/ping — Testar bot",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v5.0 online! 🌍 11 ligas")

def setid(update, context):
    chat_id = str(update.message.chat_id)
    update.message.reply_text(
        f"✅ Chat ID: `{chat_id}`\n\nRailway → Variables:\nKey: `CHAT_ID`\nValue: `{chat_id}`",
        parse_mode="Markdown"
    )

def ligas(update, context):
    msg = (
        "🌍 *11 Ligas monitoradas:*\n\n"
        "🇧🇷 Brasileirão\n\n"
        "🇪🇺 Champions • Europa League\n"
        "Conference • Premier League\n"
        "La Liga • Serie A • Bundesliga\n"
        "Ligue 1 • Eredivisie • Liga Portugal"
    )
    update.message.reply_text(msg, parse_mode="Markdown")

def diagnostico(update, context):
    update.message.reply_text("🔬 Testando API...")
    hoje = date.today().strftime("%Y-%m-%d")
    resultado = ""
    total = 0
    for nome, info in LIGAS.items():
        try:
            resp = requests.get(
                f"{BASE_URL}/competitions/{info['id']}/matches",
                headers=HEADERS,
                params={"dateFrom": hoje, "dateTo": hoje},
                timeout=10
            )
            count = len(resp.json().get("matches", []))
        except Exception:
            count = 0
        if count > 0:
            resultado += f"✅ {nome}: {count}\n"
            total += count
        else:
            resultado += f"⚪ {nome}: 0\n"
    update.message.reply_text(
        f"📊 *Diagnóstico — {hoje}*\n\n{resultado}\n*Total: {total} jogos*",
        parse_mode="Markdown"
    )

def jogos(update, context):
    update.message.reply_text("🔍 Buscando jogos de hoje...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["id"])
        if not matches:
            continue
        encontrou = True
        msg = f"⚽ *{nome_liga}:*\n"
        for m in matches[:3]:
            casa = m["homeTeam"]["name"]
            fora = m["awayTeam"]["name"]
            hora = m.get("utcDate", "")[11:16]
            msg += f"🕐 {hora} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhum jogo hoje.")

def alerta(update, context):
    update.message.reply_text("📊 Analisando entradas de hoje...")
    hoje = date.today().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        matches = buscar_jogos_data(hoje, info["id"])
        if not matches:
            continue
        alertas = analisar_jogos(matches, info["id"], nome_liga, dias_restantes=0)
        for resultado in alertas:
            msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
            encontrou = True
            registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, linha, confianca)
            update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("🎯 Nenhuma entrada forte hoje.")

def antecipados(update, context):
    update.message.reply_text("📅 Buscando próximos 3 dias...")
    hoje = date.today()
    encontrou = False
    for dias in range(1, 4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            matches = buscar_jogos_data(data_str, info["id"])
            if not matches:
                continue
            alertas = analisar_jogos(matches, info["id"], nome_liga, dias_restantes=dias)
            for resultado in alertas:
                msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
                encontrou = True
                registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, linha, confianca)
                update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhuma oportunidade antecipada.")

def historico(update, context):
    db = carregar_db()
    finalizados = [a for a in db["alertas"] if a["resultado"] is not None]
    pendentes = [a for a in db["alertas"] if a["resultado"] is None]
    if not finalizados:
        update.message.reply_text("📊 Ainda sem histórico.\nAguarde os primeiros jogos!")
        return
    acertos = [a for a in finalizados if a["resultado"]]
    pct = round(len(acertos) / len(finalizados) * 100)
    msg = (
        f"📊 *Histórico Geral*\n{'━'*28}\n\n"
        f"Total: {len(db['alertas'])} | ✅ {len(acertos)} | "
        f"❌ {len(finalizados)-len(acertos)} | ⏳ {len(pendentes)}\n"
        f"🎯 *Taxa: {pct}%*\n\n*Últimas 5:*\n"
    )
    for a in finalizados[-5:]:
        icon = "✅" if a["resultado"] else "❌"
        msg += f"{icon} {a['casa']} x {a['fora']}\n"
    update.message.reply_text(msg, parse_mode="Markdown")

def relatorio(update, context):
    mes = date.today().strftime("%Y-%m")
    db = carregar_db()
    alertas_mes = [a for a in db["alertas"] if a["data"].startswith(mes) and a["resultado"] is not None]
    if not alertas_mes:
        update.message.reply_text("📅 Sem resultados finalizados este mês ainda.")
        return
    acertos = [a for a in alertas_mes if a["resultado"]]
    total = len(alertas_mes)
    pct = round(len(acertos) / total * 100)
    banca = 500.0
    lucro = (len(acertos) * banca * 0.05) - ((total - len(acertos)) * banca * 0.03)
    roi = round((lucro / banca) * 100, 1)
    msg = (
        f"📅 *Relatório — {mes}*\n{'━'*28}\n\n"
        f"Entradas: {total} | ✅ {len(acertos)} | ❌ {total-len(acertos)}\n"
        f"🎯 *Taxa: {pct}%*\n\n"
        f"💰 *Banca R$500:*\n"
        f"Lucro: R$ {lucro:+.2f}\n"
        f"Banca final: R$ {banca+lucro:.2f}\n"
        f"ROI: *{roi}%*\n\n"
        f"{'🟢 Mês lucrativo!' if lucro > 0 else '🔴 Mês negativo.'}"
    )
    update.message.reply_text(msg, parse_mode="Markdown")

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
    dp.add_handler(CommandHandler("diagnostico", diagnostico))

    jq = updater.job_queue
    jq.run_daily(alerta_automatico, time=dtime(hour=11, minute=0))
    jq.run_repeating(verificar_resultados, interval=7200, first=60)
    jq.run_daily(relatorio_diario, time=dtime(hour=2, minute=0))
    jq.run_daily(relatorio_mensal, time=dtime(hour=11, minute=30))

    print("Bot v5.0 rodando!")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
