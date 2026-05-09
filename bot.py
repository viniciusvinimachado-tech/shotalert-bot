import os
import json
import logging
import requests
import time
from datetime import date, timedelta, time as dtime, datetime, timezone
from flask import Flask, request
from telegram.ext import Updater, CommandHandler
from telegram import Bot, Update

TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
API_KEY = os.environ.get("API_KEY") # Tira da variável, não deixa hardcoded

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

# ─── SEASON CORRIGIDA ─────────────────────────────────────────────────────────
def get_season(league_id):
    # USA UTC PRA EVITAR BUG DE FUSO
    hoje = datetime.now(timezone.utc).date()
    ano = hoje.year
    mes = hoje.month

    ligas_europa = ["2", "3", "848", "39", "140", "135", "78", "61", "88", "94"]
    ligas_anuais = ["71", "73", "13", "11", "128"] # Brasil + Argentina
    ligas_mls = ["253", "262"] # MLS e Liga MX começam em fev/mar

    if str(league_id) in ligas_europa:
        return str(ano - 1) if mes < 7 else str(ano)

    if str(league_id) in ligas_mls:
        # MLS 2026 começa em fev/2026. Se estamos em dez/2025, ainda é 2025
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

# ─── DB + CACHE PERSISTENTE ───────────────────────────────────────────────────
DB_FILE = "historico.json"
CACHE_FILE = "cache.json"

def carregar_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"alertas": []}

def salvar_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

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
    if item and time.time() - item["time"] < 3600: # 1h de cache
        return item["data"]
    return None

def cache_set(key, value):
    _cache[key] = {"data": value, "time": time.time()}
    salvar_cache(_cache)

def registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, linha, confianca):
    db = carregar_db()
    if not any(a["fixture_id"] == fixture_id for a in db["alertas"]):
        db["alertas"].append({
            "fixture_id": fixture_id,
            "casa": casa, "fora": fora, "liga": liga,
            "data": data_jogo, "hora": hora,
            "linha": linha, "confianca": confianca,
            "resultado": None, "fin_real": None,
            "registrado_em": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
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

# ─── REQUEST COM RETRY ────────────────────────────────────────────────────────
def api_request(endpoint, params, max_retries=3):
    url = f"{BASE_URL}/{endpoint}"
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=15)
            data = resp.json()

            # LOG DE ERRO DA API
            if data.get("errors"):
                logging.error(f"API ERR {endpoint}: {data['errors']}")
                if "rate limit" in str(data["errors"]).lower():
                    time.sleep(2 ** attempt) # Backoff exponencial
                    continue

            if resp.status_code == 429:
                logging.warning(f"Rate limit. Tentativa {attempt+1}/{max_retries}")
                time.sleep(2 ** attempt)
                continue

            return data.get("response", [])
        except Exception as e:
            logging.error(f"Exception {endpoint} tentativa {attempt+1}: {e}")
            time.sleep(1)
    return []

# ─── FUNÇÕES DE BUSCA ─────────────────────────────────────────────────────────
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

    params = {"team": team_id, "league": league_id, "season": season, "page": "1"}
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

def buscar_resultado_jogo(fixture_id):
    params = {"id": fixture_id}
    data = api_request("fixtures", params)
    if not data:
        return None, None
    jogo = data[0]
    status = jogo.get("fixture", {}).get("status", {}).get("short", "")
    if status not in ["FT", "AET", "PEN"]:
        return None, None
    stats = jogo.get("statistics", [])
    fin_casa = fin_fora = 0
    for s in stats:
        for item in s.get("statistics", []):
            if item.get("type") == "Total Shots":
                val = int(item.get("value") or 0)
                if s.get("team", {}).get("id") == jogo["teams"]["home"]["id"]:
                    fin_casa = val
                else:
                    fin_fora = val
    return fin_casa + fin_fora, status

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

    prefixos = {
        0: "🚨 *ALERTA HOJE*",
        1: "⚡ *ALERTA — AMANHÃ*",
        2: "📅 *ANTECIPADO — 2 DIAS*",
        3: "📅 *ANTECIPADO — 3 DIAS*"
    }
    janelas = {
        1: "\n⏰ *Entrar hoje garante odd melhor!*",
        2: "\n⏰ *Odd desregulada — janela aberta!*",
        3: "\n⏰ *Melhor momento — odd no pico!*"
    }
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
        f"💰 *Mercado: Over {linha} finalizações*\n\n"
        f"🏦 Verificar odds:\n• Superbet • Bet365\n• Betano • KTO • Novabet{janela}"
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
            fin_casa = buscar_artilheiros(id_casa, league_id, season)
            fin_fora = buscar_artilheiros(id_fora, league_id, season)
            resultado = formatar_alerta(f, stats_casa, stats_fora, fin_casa, fin_fora, dias_restantes)
            if resultado[1] >= 75:
                alertas.append(resultado)
    alertas.sort(key=lambda x: x[1], reverse=True)
    return alertas[:3]

# ─── JOBS ─────────────────────────────────────────────────────────────────────
def verificar_resultados(context):
    db = carregar_db()
    pendentes = [a for a in db["alertas"] if a["resultado"] is None]
    for alerta in pendentes:
        fin_real, status = buscar_resultado_jogo(alerta["fixture_id"])
        if fin_real is None: continue
        acerto = fin_real > alerta["linha"]
        atualizar_resultado(alerta["fixture_id"], acerto, fin_real)

def alerta_automatico(context):
    if not CHAT_ID: return
    bot = Bot(token=TOKEN)
    hoje = datetime.now(timezone.utc).date() # USA UTC
    bot.send_message(chat_id=CHAT_ID, text="🌅 *Bom dia! Buscando melhores entradas...*", parse_mode="Markdown")
    encontrou = False
    for dias in range(4):
        data_str = (hoje + timedelta(days=dias)).strftime("%Y-%m-%d")
        for nome_liga, info in LIGAS.items():
            fixtures = buscar_jogos_data(data_str, info["id"], info["season"])
            if not fixtures: continue
            alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=dias)
            for resultado in alertas:
                msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
                encontrou = True
                registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, linha, confianca)
                bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    if not encontrou:
        bot.send_message(chat_id=CHAT_ID, text="📭 Nenhuma entrada forte hoje.", parse_mode="Markdown")

def relatorio_diario(context):
    if not CHAT_ID: return
    bot = Bot(token=TOKEN)
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    db = carregar_db()
    alertas_hoje = [a for a in db["alertas"] if a["data"] == hoje and a["resultado"] is not None]
    if not alertas_hoje:
        bot.send_message(chat_id=CHAT_ID, text="📋 *Relatório:* Nenhuma entrada finalizada hoje.", parse_mode="Markdown")
        return
    acertos = [a for a in alertas_hoje if a["resultado"]]
    pct = round(len(acertos) / len(alertas_hoje) * 100)
    msg = f"📋 *Relatório Diário — {hoje}*\n{'━'*28}\n\n✅ {len(acertos)} | ❌ {len(alertas_hoje)-len(acertos)} | 🎯 *{pct}%*\n\n"
    for a in alertas_hoje:
        icon = "✅" if a["resultado"] else "❌"
        msg += f"{icon} {a['casa']} x {a['fora']} — {a['fin_real']} fin\n"
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

def relatorio_mensal(context):
    if not CHAT_ID: return
    bot = Bot(token=TOKEN)
    mes = datetime.now(timezone.utc).date().strftime("%Y-%m")
    db = carregar_db()
    alertas_mes = [a for a in db["alertas"] if a["data"].startswith(mes) and a["resultado"] is not None]
    if not alertas_mes: return
    acertos = [a for a in alertas_mes if a["resultado"]]
    total = len(alertas_mes)
    pct = round(len(acertos) / total * 100)
    banca = 500.0
    lucro = (len(acertos) * banca * 0.05) - ((total - len(acertos)) * banca * 0.03)
    roi = round((lucro / banca) * 100, 1)
    msg = (
        f"📅 *Relatório Mensal — {mes}*\n{'━'*28}\n\n"
        f"Entradas: {total} | ✅ {len(acertos)} | ❌ {total-len(acertos)}\n"
        f"🎯 *Taxa: {pct}%*\n\n💰 *Banca R$500:*\nLucro: R$ {lucro:+.2f}\n"
        f"Banca final: R$ {banca+lucro:.2f}\nROI: *{roi}%*\n\n"
        f"{'🟢 Mês lucrativo!' if lucro > 0 else '🔴 Mês negativo.'}"
    )
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ─── COMANDOS ─────────────────────────────────────────────────────────────────
def start(update, context):
    update.message.reply_text(
        "🤖 *Finalizações Bot v6.3*\n\n"
        "🌍 17 ligas • 🎯 Filtro 75%+ • 📊 Cache 1h\n"
        "📅 Season auto UTC • 🔄 Retry API\n\n"
        "/jogos — Jogos de hoje\n/alerta — Alertas de hoje\n"
        "/antecipados — Próximos 3 dias\n/historico — Histórico\n"
        "/relatorio — Relatório do mês\n/ligas — Ligas\n"
        "/diagnostico — Testar API\n/testapi — Testa API direta\n"
        "/setid — Ativar alertas\n/ping — Testar bot",
        parse_mode="Markdown"
    )

def ping(update, context):
    update.message.reply_text("✅ Bot v6.3 online! UTC | Cache | Retry")

def setid(update, context):
    chat_id = str(update.message.chat_id)
    update.message.reply_text(f"✅ Chat ID: `{chat_id}`\n\nRailway → Variables:\nKey: `CHAT_ID`\nValue: `{chat_id}`", parse_mode="Markdown")

def ligas(update, context):
    msg = "🌍 *17 Ligas monitoradas:*\n\n"
    for nome, info in LIGAS.items():
        msg += f"• {nome} - Season {info['season']}\n"
    update.message.reply_text(msg, parse_mode="Markdown")

def testapi(update, context):
    """Testa API direto pra ver se tem dado"""
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    params = {"date": hoje, "league": "39", "season": get_season("39"), "timezone": "UTC"}
    data = api_request("fixtures", params)
    update.message.reply_text(f"🔬 *Teste API Premier League*\nData: {hoje}\nSeason: {get_season('39')}\nJogos: {len(data)}", parse_mode="Markdown")

def diagnostico(update, context):
    update.message.reply_text("🔬 Testando API...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    resultado = ""
    total = 0
    for nome, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        count = len(fixtures)
        if count > 0:
            resultado += f"✅ {nome}: {count} (S{info['season']})\n"
            total += count
        else:
            resultado += f"⚪ {nome}: 0 (S{info['season']})\n"
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
        for f in fixtures[:3]:
            casa = f["teams"]["home"]["name"]
            fora = f["teams"]["away"]["name"]
            hora = f["fixture"]["date"][11:16]
            msg += f"🕐 {hora} — {casa} x {fora}\n"
        update.message.reply_text(msg, parse_mode="Markdown")
    if not encontrou:
        update.message.reply_text("📭 Nenhum jogo hoje.")

def alerta(update, context):
    update.message.reply_text("📊 Analisando entradas de hoje...")
    hoje = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    encontrou = False
    for nome_liga, info in LIGAS.items():
        fixtures = buscar_jogos_data(hoje, info["id"], info["season"])
        if not fixtures: continue
        alertas = analisar_jogos(fixtures, info["id"], info["season"], dias_restantes=0)
        for resultado in alertas:
            msg, confianca, fixture_id, linha, casa, fora, liga, data_jogo, hora = resultado
            encontrou = True
            registrar_alerta(fixture_id, casa, fora, liga, data_jogo, hora, linha, confianca)
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
    msg = (f"📊 *Histórico Geral*\n{'━'*28}\n\nTotal: {len(db['alertas'])} | ✅ {len(acertos)} | "
           f"❌ {len(finalizados)-len(acertos)} | ⏳ {len(pendentes)}\n🎯 *Taxa: {pct}%*\n\n*Últimas 5:*\n")
    for a in finalizados[-5:]:
        icon = "✅" if a["resultado"] else "❌"
        msg += f"{icon} {a['casa']} x {a['fora']} — {a['fin_real']} fin\n"
    update.message.reply_text(msg, parse_mode="Markdown")

def relatorio(update, context):
    mes = datetime.now(timezone.utc).date().strftime("%Y-%m")
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
    msg = (f"📅 *Relatório — {mes}*\n{'━'*28}\n\nEntradas: {total} | ✅ {len(acertos)} | ❌ {total-len(acertos)}\n"
           f"🎯 *Taxa: {pct}%*\n\n💰 *Banca R$500:*\nLucro: R$ {lucro:+.2f}\nBanca final: R$ {banca+lucro:.2f}\n"
           f"ROI: *{roi}%*\n\n{'🟢 Mês lucrativo!' if lucro > 0 else '🔴 Mês negativo.'}")
    update.message.reply_text(msg, parse_mode="Markdown")

# ─── FLASK + WEBHOOK ──────────────────────────────────────────────────────────
app = Flask(__name__)
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
dp.add_handler(CommandHandler("testapi", testapi))

jq = updater.job_queue
jq.run_daily(alerta_automatico, time=dtime(hour=11, minute=0)) # 11:00 UTC = 08:00 BRT
jq.run_repeating(verificar_resultados, interval=7200, first=60)
jq.run_daily(relatorio_diario, time=dtime(hour=2, minute=0)) # 02:00 UTC = 23:00 BRT
jq.run_daily(relatorio_mensal, time=dtime(hour=11, minute=30))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), updater.bot)
    dp.process_update(update)
    return 'ok'

@app.route('/')
def index():
    return 'Bot v6.3 Online!'

@app.route('/health')
def health():
    return 'ok', 200

@app.route('/setwebhook')
def setwebhook():
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    webhook_url = f"{RAILWAY_URL}/{TOKEN}"
    updater.bot.set_webhook(url=webhook_url)
    return f'Webhook setado: {webhook_url}'

def set_webhook():
    RAILWAY_URL = os.environ.get("RAILWAY_STATIC_URL")
    if RAILWAY_URL:
        updater.bot.delete_webhook(drop_pending_updates=True)
        webhook_url = f"{RAILWAY_URL}/{TOKEN}"
        updater.bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook setado: {webhook_url}")

if __name__ == "__main__":
    set_webhook()
    PORT = int(os.environ.get("PORT", 8080))
    logging.info(f"Bot v6.3 rodando — API-Football! Porta: {PORT}")
    # REMOVIDO start_polling() - webhook não usa
    app.run(host="0.0.0.0", port=PORT)
