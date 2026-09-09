#!/usr/bin/env python3
"""
Motore dei segnali v4 per Fineco: solo strumenti cash (niente CFD, niente leva di conto), una posizione alla volta.

Novita' della v4 rispetto alla v3 (tutte governate da parametri in P, con i valori v3 riproducibili):
  - stop e take profit ricalcolati sul prezzo realmente eseguito (comando ESEGUITO o deduzione dai prezzi), mantenendo
    le distanze in percentuale del segnale (ricalcolo_su_eseguito);
  - regola del gap in apertura: se il titolo apre sotto una soglia (chiusura - gap_max_atr ATR) l'ordine non va eseguito
    e il motore lo considera annullato (gap_max_atr);
  - stop iniziale "di struttura": sotto il minimo delle ultime struttura_n sedute meno struttura_buffer_atr ATR, se piu'
    largo dello stop a sl_atr ATR, entro sl_max (stop_struttura);
  - stop mobile alla chandelier: massimo dall'ingresso meno trailing_atr ATR (trailing_base="hh"), oppure come v3 dalla
    chiusura (trailing_base="close");
  - filtro sui gap storici: escluso chi ha avuto un gap giornaliero oltre gap_storico_max negli ultimi 90 giorni;
  - classifica alternativa "clenow": pendenza della regressione esponenziale a 90 giorni annualizzata per R quadro;
  - take profit facoltativo (usa_tp): senza, l'uscita e' solo con lo stop mobile e il time stop;
  - gli esiti dei comandi del canale finiscono nel messaggio serale (stato["eventi_pendenti"]).
Il messaggio serale riporta la soglia di gap e la tabella stop/take profit per i possibili prezzi di esecuzione,
cosi' Ugo puo' inserire gli ordini di protezione subito dopo l'acquisto senza aspettare la sera.

  python3 segnali_v4.py analizza --dati URL_PREZZI --strumenti URL_STRUMENTI --stato stato.json [--oggi YYYY-MM-DD] [--out analisi.json]
  python3 segnali_v4.py leggi_canale --stato stato.json      (TG_TOKEN e TG_CHAT nell'ambiente)
  python3 segnali_v4.py invia --messaggio file.txt
  python3 segnali_v4.py init --stato stato.json

Tre livelli di strumenti, in ordine di preferenza (il costo entra nella classifica):
  a) etf_promo  ETF della lista promo Fineco: acquisto 0, vendita 2,95
  b) etf        altri ETF su Borsa Italiana: 2,95 + 2,95
  c) azione     azioni Borsa Italiana (.MI, con Tobin tax 0,1% in acquisto) e Xetra (.DE): 2,95 + 2,95
Le regole complete sono in claude/strategia-v4.md.
"""
import argparse, json, math, os, urllib.request, urllib.parse, datetime as dt
import numpy as np
import pandas as pd

P = dict(
    capitale_iniziale=500.0,
    rischio_pct=0.07,           # perdita massima per operazione (sullo stop) in % dell'equity
    rischio_pct_ridotto=0.035,  # sotto la soglia morbida il rischio si dimezza
    soglia_morbida=0.85,        # -15%: rischio dimezzato
    kill_switch=0.70,           # -30%: strategia sospesa
    max_ingressi_settimana=3,
    quota_max=1.0,              # cash massimo impiegabile in una posizione (in % dell'equity)
    impiego_min=0.80,           # si salta lo strumento se i pezzi interi non impiegano almeno l'80% della quota
    commissione=2.95,           # Fineco 0,19% min 2,95: sotto 1.550 euro scatta sempre il minimo
    tobin=0.001,                # 0,1% sull'acquisto di azioni italiane
    time_stop=30,
    # segnali
    atr_n=14, sl_atr=2.0,
    sl_min=0.025, sl_max=0.06, atr_pct_max=0.03,   # strumenti troppo nervosi restano fuori: nei test riducono la discesa massima senza togliere rendimento
    rr=2.0, tp_max=0.15,
    estensione_max=2.5,
    limite_ingresso=0.004, limite_uscita=0.006,
    ban_giorni=10, ban_non_disponibile=60,
    trailing_min_step=0.01,
    ranking="vol",              # "vol": momentum diviso volatilita' (piu' stabile nei test); "puro": momentum semplice; "clenow": regressione esponenziale 90gg x R2
    # novita' v4 (con questi valori a False/0 il motore si comporta come la v3)
    ricalcolo_su_eseguito=True, # stop e take profit ricalcolati sul prezzo eseguito, stesse distanze percentuali del segnale
    gap_max_atr=0.5,            # non comprare se l'apertura e' sotto chiusura - 0,5 ATR (0 = regola spenta)
    stop_struttura=False, struttura_n=10, struttura_buffer_atr=0.5,  # stop sotto il minimo delle ultime 10 sedute meno 0,5 ATR (spento: nei test peggiora, vedi strategia v4)
    trailing_base="hh", trailing_atr=3.0,   # chandelier: massimo dall'ingresso meno 3 ATR ("close": come v3, chiusura meno sl_atr ATR)
    gap_storico_max=0,          # escluso chi ha avuto un gap giornaliero oltre questa soglia negli ultimi 90 giorni (0 = spento: nei test peggiora)
    usa_tp=True,                # False: nessun ordine di take profit, uscita con stop mobile e time stop
    penalita={"etf_promo": 0.0, "etf": 1.0, "azione": 1.5},   # sottratta al punteggio: fa preferire i livelli a costo minore
    liq_min={"etf_promo": 20_000.0, "etf": 50_000.0, "azione": 2_000_000.0}, storia_min=210,
)
TICKER_REGIME = "SWDA"
DIFENSIVI = ("GOLD", "EM15", "XUTD", "SGLD", "PHAU", "IDTL", "XGLD")   # ammessi in regime OFF (oro, governativi lunghi)

STATO_INIZIALE = {"versione": 4, "capitale_iniziale": P["capitale_iniziale"], "cash": P["capitale_iniziale"],
                  "posizioni": [], "ordini_pendenti": [], "uscite_pendenti": [], "ban": {}, "storico": [],
                  "attivo": True, "note": [], "eventi_pendenti": [], "tg_last_update_id": 0}

# ----------------------------------------------------------------------------- utilita'
def tick(p):
    p = float(p); return round(p, 3) if p < 10 else round(p, 2)

def eur(x):
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

def leggi(sorgente):
    if sorgente.startswith("http"):
        with urllib.request.urlopen(sorgente, timeout=90) as r: return pd.read_csv(r)
    return pd.read_csv(sorgente)

def carica(dati, strumenti):
    df = leggi(dati); df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.dropna(subset=["close"]).sort_values(["ticker", "date"])
    s = leggi(strumenti).fillna("")
    meta = {r["ticker"]: dict(tipo=r["tipo"], isin=r["isin"], nome=r["nome"] or r["ticker"], mercato=r.get("mercato", "") or ("Xetra" if str(r.get("yahoo", "")).endswith(".DE") else "Borsa Italiana")) for _, r in s.iterrows()}
    return df, meta

def indicatori(g):
    g = g.copy(); c = g["close"]
    g["sma20"] = c.rolling(20).mean(); g["sma50"] = c.rolling(50).mean(); g["sma200"] = c.rolling(200).mean()
    pc = c.shift(1)
    tr = pd.concat([g["high"] - g["low"], (g["high"] - pc).abs(), (g["low"] - pc).abs()], axis=1).max(axis=1)
    g["atr"] = tr.rolling(P["atr_n"]).mean()
    g["r21"] = c / c.shift(21) - 1; g["r63"] = c / c.shift(63) - 1
    g["atr_pct"] = g["atr"] / c; g["est"] = (c - g["sma20"]) / g["sma20"]
    g["liq"] = (c * g["volume"]).rolling(60).median(); g["n"] = range(1, len(g) + 1)
    g["low_n"] = g["low"].rolling(P["struttura_n"]).min()
    g["gap90"] = (g["open"] / pc - 1).abs().rolling(90).max()
    g["clenow"] = clenow(c.values, 90)
    return g

def clenow(c, n=90):
    """Pendenza della regressione lineare di log(prezzo) su n sedute, annualizzata (exp(slope)^252 - 1), per R quadro."""
    y = np.log(np.asarray(c, dtype=float)); out = np.full(len(y), np.nan)
    if len(y) < n: return out
    x = np.arange(n, dtype=float); xc = x - x.mean(); sxx = float((xc ** 2).sum())
    sxy = np.convolve(y, xc[::-1], mode="valid")                      # somma (x-xm)*y sulla finestra = somma (x-xm)(y-ym)
    ym = pd.Series(y).rolling(n).mean().values[n - 1:]
    syy = (pd.Series(y).rolling(n).var(ddof=0).values[n - 1:]) * n
    slope = sxy / sxx
    with np.errstate(divide="ignore", invalid="ignore"):
        r2 = np.where(syy > 0, sxy ** 2 / (sxx * syy), 0.0)
    out[n - 1:] = (np.exp(slope) ** 252 - 1) * r2
    return out

_cache = {}
def serie(df, ticker):
    if ticker not in _cache: _cache[ticker] = indicatori(df[df["ticker"] == ticker])
    return _cache[ticker]

def ultimo(df, ticker, oggi):
    g = serie(df, ticker); g = g[g["date"] <= oggi]
    return None if g.empty else g.iloc[-1]

def barra_dopo(df, ticker, data):
    g = serie(df, ticker); g = g[g["date"] > data]
    return None if g.empty else g.iloc[0]

def sedute_tra(df, ticker, d1, d2, inclusivo=True):
    g = serie(df, ticker); m = (g["date"] >= d1) if inclusivo else (g["date"] > d1)
    return int((m & (g["date"] <= d2)).sum())

def regime(df, oggi):
    r = ultimo(df, TICKER_REGIME, oggi)
    if r is None or math.isnan(r["sma200"]): return "sconosciuto", r
    return ("on" if r["close"] > r["sma200"] else "off"), r

# ----------------------------------------------------------------------------- telegram
def tg_api(metodo, **params):
    token = os.environ.get("TG_TOKEN")
    if not token: raise SystemExit("TG_TOKEN mancante")
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/{metodo}", data=data), timeout=30) as r:
        return json.load(r)

def invia(testo):
    chat = os.environ.get("TG_CHAT")
    if not chat: raise SystemExit("TG_CHAT mancante")
    return tg_api("sendMessage", chat_id=chat, text=testo, parse_mode="HTML", disable_web_page_preview="true")

def leggi_canale(stato):
    chat = str(os.environ.get("TG_CHAT", ""))
    res = tg_api("getUpdates", offset=stato.get("tg_last_update_id", 0) + 1, timeout=0, allowed_updates='["channel_post","message"]')
    comandi = []
    for u in res.get("result", []):
        stato["tg_last_update_id"] = max(stato.get("tg_last_update_id", 0), u["update_id"])
        m = u.get("channel_post") or u.get("message")
        if not m or "text" not in m: continue
        cid = str(m["chat"]["id"]); uname = "@" + m["chat"].get("username", "") if m["chat"].get("username") else ""
        if chat and cid != chat and uname.lower() != chat.lower(): continue
        comandi.append({"data": dt.datetime.fromtimestamp(m["date"]).date().isoformat(), "testo": m["text"].strip()})
    return comandi

def _trova(lista, ticker):
    for x in lista:
        if ticker and x["ticker"].upper() == ticker.upper(): return x
    return None

def applica_comandi(stato, comandi):
    """ESEGUITO [ticker] qta prezzo · NON ESEGUITO [ticker] · VENDUTO|CHIUSO [ticker] qta prezzo
       NON DISPONIBILE ticker · SALDO euro · PAUSA · RIPRENDI   (ticker omettibile: c'e' al massimo un ordine e una posizione)"""
    esiti = []
    for c in comandi:
        t = c["testo"].upper().replace(",", "."); parti = t.split()
        n0 = len(esiti)
        if not parti: continue
        try:
            if t.startswith("NON ESEGUITO"):
                o = stato["ordini_pendenti"][0] if stato["ordini_pendenti"] else None
                if o: stato["ordini_pendenti"].remove(o); esiti.append(f"ordine su {o['ticker']} annullato su tua conferma")
                else: esiti.append("NON ESEGUITO: nessun ordine pendente")
            elif t.startswith("NON DISPONIBILE"):
                tk = parti[2]; o = _trova(stato["ordini_pendenti"], tk)
                if o: stato["ordini_pendenti"].remove(o)
                stato["ban"][tk] = {"data": c["data"], "giorni": P["ban_non_disponibile"]}
                esiti.append(f"{tk} segnato come non disponibile: escluso per {P['ban_non_disponibile']} sedute")
            elif parti[0] == "ESEGUITO":
                nums = [x for x in parti[1:] if x.replace(".", "", 1).isdigit()]
                qta, prezzo = int(float(nums[-2])), float(nums[-1])
                o = stato["ordini_pendenti"][0] if stato["ordini_pendenti"] else None
                if o:
                    p = apri_posizione(stato, o, qta, prezzo, c["data"], "confermato")
                    esiti.append(f"ingresso confermato: {o['ticker']} {qta} a {prezzo}. Stop {p['sl']}" + (f", take profit {p['tp']}" if p.get("tp") else ", nessun take profit") + (" (ricalcolati sul prezzo eseguito)" if P["ricalcolo_su_eseguito"] else ""))
                else: esiti.append("ESEGUITO: nessun ordine pendente")
            elif parti[0] in ("VENDUTO", "CHIUSO"):
                nums = [x for x in parti[1:] if x.replace(".", "", 1).isdigit()]
                qta, prezzo = int(float(nums[-2])), float(nums[-1])
                p = stato["posizioni"][0] if stato["posizioni"] else None
                if p: chiudi_posizione(stato, p, prezzo, c["data"], "confermato da te"); esiti.append(f"vendita confermata: {p['ticker']} {qta} a {prezzo}")
                else: esiti.append(f"{parti[0]}: nessuna posizione aperta")
            elif parti[0] == "SALDO": stato["cash"] = float(parti[1]); esiti.append(f"cash allineato a {parti[1]}")
            elif parti[0] == "PAUSA": stato["attivo"] = False; esiti.append("segnali sospesi")
            elif parti[0] == "RIPRENDI": stato["attivo"] = True; esiti.append("segnali riattivati")
        except Exception as e:
            esiti.append(f"comando non capito: '{c['testo']}' ({e})")
        stato.setdefault("eventi_pendenti", []).extend(f"canale {c['data']} '{c['testo']}': {x}" for x in esiti[n0:])
    return esiti

# ----------------------------------------------------------------------------- posizioni e costi
def costo_acquisto(tipo, mercato, controvalore):
    c = 0.0 if tipo == "etf_promo" else P["commissione"]
    if tipo == "azione" and mercato == "Borsa Italiana": c += controvalore * P["tobin"]
    return c

def livelli(prezzo, dist_sl, tp_pct):
    """Stop e take profit per un dato prezzo di esecuzione, con le distanze percentuali del segnale."""
    sl = tick(prezzo * (1 - dist_sl)); tp = tick(prezzo * (1 + tp_pct)) if P["usa_tp"] and tp_pct else None
    return sl, tp

def apri_posizione(stato, ordine, qta, prezzo, data, fonte):
    prezzo = float(prezzo)
    pos = {k: ordine[k] for k in ("ticker", "tipo", "nome", "isin", "mercato", "sl", "tp", "atr")}
    if P["ricalcolo_su_eseguito"]:
        pos["sl"], pos["tp"] = livelli(prezzo, ordine["dist_sl"], ordine.get("tp_pct", 0.0))
        pos["sl_segnale"], pos["tp_segnale"] = ordine["sl"], ordine["tp"]
    if not P["usa_tp"]: pos["tp"] = None
    ca = costo_acquisto(ordine["tipo"], ordine["mercato"], qta * prezzo)
    pos.update(qta=qta, prezzo=prezzo, data=data, rischio_1r=prezzo - pos["sl"], fonte_eseguito=fonte,
               sl_iniziale=pos["sl"], costo_apertura=round(ca, 2), max_dall_ingresso=prezzo)
    stato["cash"] -= qta * prezzo + ca
    stato["posizioni"].append(pos)
    if ordine in stato["ordini_pendenti"]: stato["ordini_pendenti"].remove(ordine)
    return pos

def chiudi_posizione(stato, p, prezzo, data, motivo):
    prezzo = float(prezzo); cc = P["commissione"]
    pl = p["qta"] * (prezzo - p["prezzo"]) - cc - p["costo_apertura"]
    stato["cash"] += p["qta"] * prezzo - cc
    stato["storico"].append({**p, "prezzo_uscita": prezzo, "data_uscita": data, "motivo": motivo,
                             "costo_totale": round(cc + p["costo_apertura"], 2), "pl_netto": round(pl, 2),
                             "pl_pct": round(pl / (p["qta"] * p["prezzo"]) * 100, 2)})
    stato["ban"][p["ticker"]] = {"data": data, "giorni": P["ban_giorni"]}
    stato["posizioni"].remove(p)
    stato["uscite_pendenti"] = [u for u in stato["uscite_pendenti"] if u["ticker"] != p["ticker"]]
    return pl

def equity(stato, df, oggi):
    e = stato["cash"]
    for p in stato["posizioni"]:
        r = ultimo(df, p["ticker"], oggi); e += p["qta"] * (float(r["close"]) if r is not None else p["prezzo"])
    return e

def in_ban(stato, ticker, oggi, df):
    b = stato["ban"].get(ticker)
    if not b: return False
    if isinstance(b, str): b = {"data": b, "giorni": P["ban_giorni"]}
    return sedute_tra(df, ticker, dt.date.fromisoformat(b["data"]), oggi, inclusivo=False) < b["giorni"]

def ingressi_settimana(stato, oggi):
    lun = oggi - dt.timedelta(days=oggi.weekday())
    n = sum(1 for x in stato["posizioni"] + stato["storico"] if dt.date.fromisoformat(x["data"]) >= lun)
    return n + sum(1 for o in stato["ordini_pendenti"] if dt.date.fromisoformat(o["data_segnale"]) >= lun)

# ----------------------------------------------------------------------------- selezione
def candidati(df, meta, oggi, stato, reg):
    aperti = {p["ticker"] for p in stato["posizioni"]} | {o["ticker"] for o in stato["ordini_pendenti"]}
    righe = []
    for t, m in meta.items():
        if t in aperti or t == TICKER_REGIME: continue
        r = ultimo(df, t, oggi)
        if r is None or (oggi - r["date"]).days > 4: continue
        if r["n"] < P["storia_min"] or any(math.isnan(r[k]) for k in ("sma20", "sma50", "sma200", "atr", "r21", "r63", "liq", "low_n")): continue
        c = float(r["close"]); tipo = m["tipo"]; motivi = []
        if reg == "off" and t not in DIFENSIVI: motivi.append("regime off")
        if not (c > r["sma50"] > r["sma200"] and r["sma20"] > r["sma50"]): motivi.append("trend")
        if not (r["r21"] > 0 and r["r63"] > 0): motivi.append("momentum")
        if r["est"] > P["estensione_max"] * r["atr_pct"]: motivi.append("esteso")
        if r["liq"] < P["liq_min"].get(tipo, 0): motivi.append("illiquido")
        if in_ban(stato, t, oggi, df): motivi.append("ban")
        if r["atr_pct"] > P["atr_pct_max"]: motivi.append("volatilita")
        if P["sl_atr"] * r["atr_pct"] > P["sl_max"]: motivi.append("stop troppo largo")
        if P["gap_storico_max"] and not math.isnan(r["gap90"]) and r["gap90"] > P["gap_storico_max"]: motivi.append("gap")
        score = 0.5 * float(r["r63"]) + 0.5 * float(r["r21"])
        if P["ranking"] == "clenow":
            score = float(r["clenow"]) if not math.isnan(r["clenow"]) else 0.0; base = score * 20
        else:
            base = score / max(float(r["atr_pct"]), 0.005) if P["ranking"] == "vol" else score * 100
        score_adj = base - P["penalita"].get(tipo, 0.0)
        righe.append(dict(ticker=t, tipo=tipo, nome=m["nome"], isin=m["isin"], mercato=m["mercato"], close=c, score=score, score_adj=score_adj,
                          r21=float(r["r21"]), r63=float(r["r63"]), atr=float(r["atr"]), atr_pct=float(r["atr_pct"]), est=float(r["est"]), low_n=float(r["low_n"]),
                          liq=float(r["liq"]), ok=not motivi, motivi=",".join(motivi)))
    righe.sort(key=lambda x: x["score_adj"], reverse=True)
    return righe

def rischio_euro(stato, eq):
    pct = P["rischio_pct"] if eq >= stato["capitale_iniziale"] * P["soglia_morbida"] else P["rischio_pct_ridotto"]
    return eq * pct, pct

def proposta(stato, cand, eq):
    c = cand["close"]
    dist = P["sl_atr"] * cand["atr"] / c
    if P["stop_struttura"] and cand.get("low_n"):
        dist = max(dist, (c - (cand["low_n"] - P["struttura_buffer_atr"] * cand["atr"])) / c)
    dist = min(max(dist, P["sl_min"]), P["sl_max"])
    limite = tick(c * (1 + P["limite_ingresso"])); sl = tick(c * (1 - dist))
    tp_dist = min(P["rr"] * dist, P["tp_max"]); tp = tick(c * (1 + tp_dist)) if P["usa_tp"] else None
    gap_soglia = tick(c * (1 - P["gap_max_atr"] * cand["atr_pct"])) if P["gap_max_atr"] else None
    rischio, pct = rischio_euro(stato, eq)
    quota_base = min(stato["cash"] - P["commissione"], eq * P["quota_max"])
    quota = min(quota_base, rischio / dist)
    qta = int(quota // limite)
    if qta < 1 or qta * limite < P["impiego_min"] * quota: return None
    noz = qta * limite
    costo = costo_acquisto(cand["tipo"], cand["mercato"], noz) + P["commissione"]
    return dict(ticker=cand["ticker"], tipo=cand["tipo"], nome=cand["nome"], isin=cand["isin"], mercato=cand["mercato"], qta=qta,
                limite=limite, close=c, sl=sl, tp=tp, atr=cand["atr"], controvalore=round(noz, 2),
                rischio_euro=round(qta * (c - sl) + costo, 2), rischio_pct=pct, dist_sl=dist, tp_pct=tp_dist if P["usa_tp"] else 0.0,
                costo_stimato=round(costo, 2), guadagno_tp=round(qta * (tp - c) - costo, 2) if tp else None, gap_soglia=gap_soglia,
                low_n=cand.get("low_n"))

# ----------------------------------------------------------------------------- ciclo
def analizza(df, meta, stato, oggi):
    eventi, istruzioni = list(stato.pop("eventi_pendenti", [])), []
    if oggi is None: oggi = df["date"].max()
    for o in list(stato["ordini_pendenti"]):
        b = barra_dopo(df, o["ticker"], dt.date.fromisoformat(o["data_segnale"]))
        if b is None: continue
        if o.get("gap_soglia") and b["open"] < o["gap_soglia"]:
            stato["ordini_pendenti"].remove(o)
            eventi.append(f"{o['ticker']} ha aperto a {tick(b['open'])}, sotto la soglia di gap {o['gap_soglia']}: ordine considerato NON eseguito. Se invece l'hai comprato scrivi ESEGUITO qta prezzo")
        elif b["low"] <= o["limite"]:
            prezzo = tick(min(b["open"], o["limite"]))
            p = apri_posizione(stato, o, o["qta"], prezzo, b["date"].isoformat(), "dedotto dai prezzi")
            eventi.append(f"acquisto {o['ticker']} considerato eseguito a {prezzo} (dedotto dai prezzi del {b['date']}): stop {p['sl']}" + (f", take profit {p['tp']}" if p.get('tp') else "") + ". Se il prezzo reale e' diverso scrivi ESEGUITO qta prezzo")
        else:
            stato["ordini_pendenti"].remove(o)
            eventi.append(f"ordine limite su {o['ticker']} a {o['limite']} non eseguito (minimo {tick(b['low'])}): annullato")
    for u in list(stato["uscite_pendenti"]):
        p = _trova(stato["posizioni"], u["ticker"])
        if not p: stato["uscite_pendenti"].remove(u); continue
        b = barra_dopo(df, u["ticker"], dt.date.fromisoformat(u["data_segnale"]))
        if b is None: continue
        prezzo = tick(b["open"]) if b["open"] >= u["limite"] else (tick(u["limite"]) if b["high"] >= u["limite"] else tick(b["close"]))
        pl = chiudi_posizione(stato, p, prezzo, b["date"].isoformat(), u["motivo"])
        eventi.append(f"vendita {u['ticker']} registrata a {prezzo} ({u['motivo']}), P/L netto {eur(pl)}")
    for p in list(stato["posizioni"]):
        da = dt.date.fromisoformat(p.get("verificato_fino") or p["data"]); inclusa = "verificato_fino" not in p
        g = serie(df, p["ticker"]); g = g[((g["date"] > da) | (inclusa & (g["date"] == da))) & (g["date"] <= oggi)]
        chiusa = False
        for _, b in g.iterrows():
            if b["low"] <= p["sl"]:
                pl = chiudi_posizione(stato, p, p["sl"], b["date"].isoformat(), "stop loss")
                eventi.append(f"STOP LOSS {p['ticker']} scattato il {b['date']} a {p['sl']}, P/L netto {eur(pl)}. Se non e' avvenuto scrivi VENDUTO qta prezzo o SALDO euro")
                chiusa = True; break
            if p.get("tp") and b["high"] >= p["tp"]:
                pl = chiudi_posizione(stato, p, p["tp"], b["date"].isoformat(), "take profit")
                eventi.append(f"TAKE PROFIT {p['ticker']} raggiunto il {b['date']} a {p['tp']}, P/L netto {eur(pl)}")
                chiusa = True; break
        if chiusa: continue
        p["verificato_fino"] = oggi.isoformat()
        if not g.empty: p["max_dall_ingresso"] = float(max(p.get("max_dall_ingresso", p["prezzo"]), g["high"].max()))
        r = ultimo(df, p["ticker"], oggi); c = float(r["close"])
        sedute = sedute_tra(df, p["ticker"], dt.date.fromisoformat(p["data"]), oggi)
        p["sedute"] = sedute; p["ultimo"] = c
        p["pl_aperto"] = round(p["qta"] * (c - p["prezzo"]) - P["commissione"] - p["costo_apertura"], 2)
        nuovo = p["sl"]
        if c - p["prezzo"] >= p["rischio_1r"]: nuovo = max(nuovo, p["prezzo"] + (p["costo_apertura"] + P["commissione"]) / p["qta"])
        base_trail = (p.get("max_dall_ingresso", c) - P["trailing_atr"] * float(r["atr"])) if P["trailing_base"] == "hh" else (c - P["sl_atr"] * float(r["atr"]))
        nuovo = tick(min(max(nuovo, base_trail), c * 0.995))
        if sedute >= 3 and nuovo >= p["sl"] * (1 + P["trailing_min_step"]):
            istruzioni.append(dict(tipo="modifica_stop", ticker=p["ticker"], qta=p["qta"], vecchio_sl=p["sl"], nuovo_sl=nuovo)); p["sl"] = nuovo
        if sedute >= P["time_stop"] and c < r["sma20"] and not _trova(stato["uscite_pendenti"], p["ticker"]):
            limite = tick(c * (1 - P["limite_uscita"]))
            stato["uscite_pendenti"].append(dict(ticker=p["ticker"], qta=p["qta"], limite=limite, motivo="time stop", data_segnale=oggi.isoformat()))
            istruzioni.append(dict(tipo="vendi", ticker=p["ticker"], qta=p["qta"], limite=limite, motivo=f"time stop: {P['time_stop']} sedute senza take profit e prezzo sotto la media a 20 giorni"))
    eq = equity(stato, df, oggi)
    if eq <= stato["capitale_iniziale"] * P["kill_switch"] and stato["attivo"]:
        stato["attivo"] = False; eventi.append(f"KILL SWITCH: equity {eur(eq)} sotto la soglia del -30%. Segnali sospesi, da rivedere insieme")
    reg, _ = regime(df, oggi)
    cands = candidati(df, meta, oggi, stato, reg)
    pr = None
    if stato["attivo"] and not stato["posizioni"] and not stato["ordini_pendenti"] and ingressi_settimana(stato, oggi) < P["max_ingressi_settimana"]:
        for cnd in cands:
            if not cnd["ok"]: continue
            pr = proposta(stato, cnd, eq)
            if pr: stato["ordini_pendenti"].append({**pr, "data_segnale": oggi.isoformat()}); break
    return dict(oggi=oggi.isoformat(), regime=reg, equity=round(eq, 2), cash=round(stato["cash"], 2), eventi=eventi, istruzioni=istruzioni,
                proposta=pr, classifica=cands[:10], posizioni=stato["posizioni"], attivo=stato["attivo"], rischio_pct=rischio_euro(stato, eq)[1])

# ----------------------------------------------------------------------------- messaggio
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]; MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
def data_it(d):
    d = dt.date.fromisoformat(d) if isinstance(d, str) else d
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month-1]} {d.year}"
TIPI = {"etf_promo": "ETF in promozione (acquisto a 0)", "etf": "ETF (2,95 in acquisto)", "azione": "azione (2,95 in acquisto)"}

def bozza_messaggio(a, stato):
    L = [f"<b>Segnale Fineco · {data_it(a['oggi'])}</b>",
         f"Capitale: {eur(a['equity'])} (cash {eur(a['cash'])}) · Regime: {'rischio ON' if a['regime']=='on' else 'rischio OFF' if a['regime']=='off' else 'n.d.'}"]
    if a["rischio_pct"] < P["rischio_pct"]: L.append("Modalita' prudente: rischio per operazione dimezzato finche' il capitale non torna sopra 425 €.")
    for e in a["eventi"]: L.append(f"• {e}")
    for p in a["posizioni"]:
        L.append(""); L.append(f"<b>Posizione</b> {p['ticker']} ({p['nome']}): {p['qta']} pz da {p['prezzo']} · ultimo {tick(p.get('ultimo', p['prezzo']))} · P/L netto stimato {eur(p.get('pl_aperto', 0))} · seduta {p.get('sedute', '?')}/{P['time_stop']}")
        L.append(f"Stop {p['sl']} · " + (f"Take profit {p['tp']}" if p.get('tp') else "nessun take profit: si esce con lo stop mobile o il time stop"))
    if a["istruzioni"]:
        L.append(""); L.append("<b>DA FARE</b>")
        for i in a["istruzioni"]:
            if i["tipo"] == "modifica_stop": L.append(f"Alza lo stop su {i['ticker']} ({i['qta']} pz): da {i['vecchio_sl']} a <b>{i['nuovo_sl']}</b>." + (" Il take profit resta." if P["usa_tp"] else ""))
            else: L.append(f"Vendi {i['qta']} {i['ticker']} domani in apertura, ordine limite <b>{i['limite']}</b> validita' giornaliera ({i['motivo']}). Cancella prima stop e take profit. Poi scrivi: VENDUTO qta prezzo")
    pr = a["proposta"]
    if pr:
        L.append(""); L.append("<b>OPERAZIONE PER DOMANI</b>")
        L.append(f"Compra <b>{pr['qta']} {pr['ticker']}</b> · {pr['nome']} · {TIPI[pr['tipo']]} · {pr['mercato']}" + (f" · ISIN {pr['isin']}" if pr["isin"] else ""))
        L.append(f"Ordine <b>limite {pr['limite']}</b> (chiusura {tick(pr['close'])}), validita' giornaliera · controvalore circa {eur(pr['controvalore'])} · costi stimati {eur(pr['costo_stimato'])} andata e ritorno")
        if pr["tipo"] == "etf_promo": L.append(f"Verifica che la maschera d'ordine mostri commissione 0: se no scrivi NON DISPONIBILE {pr['ticker']}")
        if pr.get("gap_soglia"): L.append(f"<b>Non comprare se in apertura il prezzo e' sotto {pr['gap_soglia']}</b> (gap in ribasso: il segnale non vale piu').")
        if P["ricalcolo_su_eseguito"]:
            L.append(f"Stop e take profit dipendono dal prezzo che ottieni: stop = eseguito meno {pr['dist_sl']*100:.1f}%" + (f", take profit = eseguito piu' {pr['tp_pct']*100:.1f}%" if pr.get("tp") else "") + ". Tabella:")
            for etichetta, px in (("al limite", pr["limite"]), ("alla chiusura", tick(pr["close"])), ("alla soglia di gap", pr.get("gap_soglia"))):
                if px:
                    sl_, tp_ = livelli(px, pr["dist_sl"], pr["tp_pct"])
                    L.append(f"  eseguito {etichetta} {px}: stop <b>{sl_}</b>" + (f" · take profit <b>{tp_}</b>" if tp_ else ""))
            L.append("Appena eseguito inserisci l'ordine stop" + (" e il limite di take profit" if pr.get("tp") else "") + " ai livelli della tabella (o calcolati con le percentuali), validi fino a data. Stasera confermo i livelli esatti.")
        else:
            L.append(f"Appena eseguito: stop loss a <b>{pr['sl']}</b> (ordine stop)" + (f" e take profit a <b>{pr['tp']}</b> (ordine limite)" if pr.get("tp") else "") + ", validi fino a data.")
        L.append(f"Rischio massimo: circa {eur(pr['rischio_euro'])} ({pr['dist_sl']*100:.1f}% + costi)" + (f" · guadagno netto al take profit: circa {eur(pr['guadagno_tp'])} (+{pr['tp_pct']*100:.1f}%)" if pr.get("tp") else " · nessun take profit: uscita con stop mobile"))
        L.append("Se domani il limite non viene raggiunto, non inseguire: domani sera nuova valutazione.")
        L.append(f"Poi scrivi qui: ESEGUITO {pr['qta']} prezzo, oppure NON ESEGUITO")
    if not pr and not a["istruzioni"] and not a["posizioni"] and not stato["ordini_pendenti"] and stato["attivo"]:
        L.append("")
        if ingressi_settimana(stato, dt.date.fromisoformat(a["oggi"])) >= P["max_ingressi_settimana"]: L.append("Nessun nuovo ingresso: raggiunto il limite di 3 ingressi questa settimana.")
        else:
            L.append("Nessun nuovo ingresso: nessuno strumento supera i filtri oggi.")
            L.append("Primi per momentum ma esclusi: " + ", ".join(f"{c['ticker']} ({c['motivi']})" for c in a["classifica"][:4]))
    if not stato["attivo"]:
        L.append(""); L.append("<b>Strategia sospesa</b>: nessun nuovo ingresso finche' non la rivediamo. Scrivi RIPRENDI per riattivare.")
    return "\n".join(L)

# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("comando", choices=["analizza", "invia", "leggi_canale", "init"])
    ap.add_argument("--dati"); ap.add_argument("--strumenti"); ap.add_argument("--stato", default="stato.json")
    ap.add_argument("--oggi"); ap.add_argument("--messaggio"); ap.add_argument("--out")
    a = ap.parse_args()
    if a.comando == "init":
        json.dump(STATO_INIZIALE, open(a.stato, "w"), indent=2, ensure_ascii=False); print("stato creato"); return
    if a.comando == "invia":
        print(json.dumps(invia(open(a.messaggio).read()))[:300]); return
    stato = json.load(open(a.stato)) if os.path.exists(a.stato) else json.loads(json.dumps(STATO_INIZIALE))
    if a.comando == "leggi_canale":
        cmds = leggi_canale(stato); esiti = applica_comandi(stato, cmds)
        json.dump(stato, open(a.stato, "w"), indent=2, ensure_ascii=False)
        print(json.dumps(dict(comandi=cmds, esiti=esiti), ensure_ascii=False, indent=2)); return
    df, meta = carica(a.dati, a.strumenti)
    oggi = dt.date.fromisoformat(a.oggi) if a.oggi else None
    res = analizza(df, meta, stato, oggi); res["messaggio"] = bozza_messaggio(res, stato)
    json.dump(stato, open(a.stato, "w"), indent=2, ensure_ascii=False)
    out = json.dumps(res, ensure_ascii=False, indent=2, default=str)
    if a.out: open(a.out, "w").write(out)
    print(out)

if __name__ == "__main__":
    main()
