#!/usr/bin/env python3
"""Versione 5 (tre livelli: etf_promo, etf, azione). Scarica da Yahoo Finance le barre giornaliere di:
  - tutti gli ETF della lista promo Fineco (promo_isin.csv: isin,nome), risolti su Borsa Italiana (.MI) dall'ISIN;
  - gli altri ETF in etf_extra.txt e le azioni in azioni.txt (ticker Yahoo;nome), Borsa Italiana e Xetra.
Scrive data/prezzi.csv (date,ticker,open,high,low,close,volume), data/strumenti.csv (ticker,tipo,isin,nome,yahoo,mercato)
e data/log.txt. La mappa ISIN -> simbolo Yahoo e' salvata in data/mappa_ticker.json e riusata.

Novita' della v5 rispetto alla v4:
  - l'ISIN viene risolto prima sulla scheda ETFplus di Borsa Italiana (codice alfanumerico = ticker di Milano), poi con la
    ricerca Yahoo (simbolo .MI diretto, altrimenti base del simbolo estero + .MI); i candidati sono verificati in blocco su Yahoo.
    La v4 usava solo la ricerca Yahoo, che restituisce il listing primario (Xetra, Londra) e risolveva 54 ISIN su 478.
  - gli ISIN non risolti vengono ritentati nelle esecuzioni successive (al massimo 3 volte); RIGENERA_MAPPA=1 azzera la mappa.
  - corretto il bug della v4 per cui la chiusura ricavata dall'intraday veniva scritta su righe di altri strumenti
    (indici duplicati dopo la concatenazione dei blocchi): ora concat con ignore_index=True.
  - filtro di plausibilita' sulle barre (massimo/minimo incoerenti con la chiusura): le righe scartate finiscono nel log.
Se Yahoo non ha ancora la chiusura ufficiale del giorno, la ricava dall'ultimo minuto scambiato."""
import csv, datetime as dt, json, os, re, sys, time
import pandas as pd
import requests
import yfinance as yf

os.makedirs("data", exist_ok=True)
MAPPA = "data/mappa_ticker.json"
MAX_TENTATIVI = 3
mappa = {} if os.environ.get("RIGENERA_MAPPA") == "1" else (json.load(open(MAPPA)) if os.path.exists(MAPPA) else {})
oggi = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d}"
log = [f"run {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC"]

# ---------------------------------------------------------------- strumenti
etf = {}   # isin -> nome
if os.path.exists("promo_isin.csv"):
    for r in csv.DictReader(open("promo_isin.csv")):
        etf[r["isin"].strip()] = r.get("nome", "").strip()
def leggi_lista(f):
    d = {}
    if os.path.exists(f):
        for l in open(f):
            l = l.strip()
            if not l or l.startswith("#"):
                continue
            sym, _, nome = l.partition(";")
            d[sym.strip()] = nome.strip() or sym.strip()
    return d
azioni = {**leggi_lista("azioni.txt"), **leggi_lista("azioni_extra.txt")}   # yahoo -> nome
etf_extra = leggi_lista("etf_extra.txt")

# ---------------------------------------------------------------- risoluzione ISIN -> ticker di Milano
BI_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36",
        "Accept-Language": "it-IT,it;q=0.9"}
RE_CODICE = re.compile(r"Codice Alfanumerico</strong>\s*</span>\s*</td>\s*<td>\s*<span[^>]*>\s*([A-Z0-9]{2,12})\s*<")

def codice_borsa_italiana(isin):
    """Scheda ETFplus per ISIN. Ritorna il codice alfanumerico, 'NQ' se Borsa Italiana non lo quota (redirect alla lista),
    None se la pagina non ha risposto."""
    for tentativo in range(2):
        try:
            r = requests.get(f"https://www.borsaitaliana.it/borsa/etf/scheda/{isin}.html?lang=it", headers=BI_H, timeout=25)
        except Exception:
            time.sleep(3); continue
        if r.status_code == 200 and "/scheda/" not in r.url:
            return "NQ"
        m = RE_CODICE.search(r.text) if r.status_code == 200 else None
        if m:
            return m.group(1)
        time.sleep(3)
    return None

def candidati_yahoo(isin):
    """Ricerca Yahoo per ISIN: simboli .MI diretti, poi la base dei simboli esteri con suffisso .MI. Ritorna (candidati, nome)."""
    q = []
    for tentativo in range(2):
        try:
            q = yf.Search(isin, max_results=15).quotes; break
        except Exception:
            time.sleep(2)
    nome = ""
    cand = []
    for r in q:
        s = str(r.get("symbol", ""))
        if not s:
            continue
        nome = nome or r.get("longname") or r.get("shortname") or ""
        if s.endswith(".MI"):
            cand.insert(0, s)
        elif "." in s and s.split(".")[0] != isin:
            cand.append(s.split(".")[0] + ".MI")
    return list(dict.fromkeys(cand)), nome

def verifica_yahoo(simboli):
    """Ritorna l'insieme dei simboli con almeno una chiusura negli ultimi 10 giorni."""
    ok = set()
    simboli = list(dict.fromkeys(simboli))
    for i in range(0, len(simboli), 80):
        lista = simboli[i:i + 80]
        try:
            d = yf.download(lista, period="10d", interval="1d", auto_adjust=False, group_by="ticker", threads=True, progress=False)
        except Exception as e:
            log.append(f"verifica blocco {i} fallita: {e}"); continue
        for y in lista:
            try:
                h = d[y] if len(lista) > 1 else d
                if not h["Close"].dropna().empty:
                    ok.add(y)
            except KeyError:
                pass
    return ok

da_risolvere = [i for i in etf if i not in mappa or (not mappa[i].get("yahoo") and mappa[i].get("bi") != "NQ" and mappa[i].get("tentativi", 0) < MAX_TENTATIVI)]
proposte = {}   # isin -> lista candidati
for n, isin in enumerate(da_risolvere, 1):
    voce = mappa.get(isin, {})
    voce.setdefault("nome", etf[isin])
    voce["tentativi"] = voce.get("tentativi", 0) + 1
    voce["aggiornato"] = oggi
    codice = codice_borsa_italiana(isin) if voce.get("bi") in (None, "") else voce["bi"]
    if codice:
        voce["bi"] = codice
    cand = [codice + ".MI"] if codice and codice != "NQ" else []
    if codice != "NQ":
        c2, nome_y = candidati_yahoo(isin)
        cand += [c for c in c2 if c not in cand]
        if nome_y and not voce.get("nome"):
            voce["nome"] = nome_y
    proposte[isin] = cand
    mappa[isin] = voce
    if n % 25 == 0:
        json.dump(mappa, open(MAPPA, "w"), indent=1, ensure_ascii=False)
    time.sleep(0.2)
validi = verifica_yahoo([c for cand in proposte.values() for c in cand]) if proposte else set()
risolti_ora = 0
for isin, cand in proposte.items():
    scelto = next((c for c in cand if c in validi), None)
    mappa[isin]["yahoo"] = scelto
    if scelto:
        risolti_ora += 1
json.dump(mappa, open(MAPPA, "w"), indent=1, ensure_ascii=False)
n_yahoo = sum(1 for i in etf if mappa.get(i, {}).get("yahoo"))
n_nq = sum(1 for i in etf if mappa.get(i, {}).get("bi") == "NQ")
n_bi = sum(1 for i in etf if mappa.get(i, {}).get("bi") not in (None, "", "NQ"))
irrisolti = [i for i in etf if not mappa.get(i, {}).get("yahoo") and mappa.get(i, {}).get("bi") != "NQ"]
log.append(f"ETF in lista: {len(etf)}, con codice Borsa Italiana: {n_bi}, con dati Yahoo (.MI): {n_yahoo}, non quotati a Milano: {n_nq}, "
           f"irrisolti: {len(irrisolti)} (ritentati questa volta: {len(da_risolvere)}, risolti ora: {risolti_ora})")
if irrisolti:
    log.append("irrisolti: " + ", ".join(f"{i}[{mappa[i].get('bi') or '-'}]" for i in irrisolti[:60]) + (" ..." if len(irrisolti) > 60 else ""))

strumenti = []  # (ticker, tipo, isin, nome, yahoo, mercato)
visti = set()
for isin, nome in etf.items():
    y = mappa.get(isin, {}).get("yahoo")
    if y and y not in visti:
        strumenti.append((y[:-3], "etf_promo", isin, mappa[isin].get("nome") or nome, y, "Borsa Italiana")); visti.add(y)
for y, nome in etf_extra.items():
    if y not in visti:
        strumenti.append((y[:-3], "etf", "", nome, y, "Borsa Italiana")); visti.add(y)
for y, nome in azioni.items():
    if y not in visti:
        strumenti.append((y, "azione", "", nome, y, "Xetra" if y.endswith(".DE") else "Borsa Italiana")); visti.add(y)

# ---------------------------------------------------------------- download
simboli = [s[4] for s in strumenti]
pezzi = []
for i in range(0, len(simboli), 80):
    lista = simboli[i:i + 80]
    try:
        d = yf.download(lista, period="2y", interval="1d", auto_adjust=False, group_by="ticker", threads=True, progress=False)
    except Exception as e:
        log.append(f"download blocco {i} fallito: {e}")
        continue
    for y in lista:
        try:
            h = d[y] if len(lista) > 1 else d
        except KeyError:
            continue
        if h is None or h.dropna(subset=["Close"]).empty:
            continue
        h = h.reset_index()[["Date", "Open", "High", "Low", "Close", "Volume"]]
        h.columns = ["date", "open", "high", "low", "close", "volume"]
        h["date"] = pd.to_datetime(h["date"]).dt.date
        h["yahoo"] = y
        pezzi.append(h)
if not pezzi:
    print("\n".join(log)); sys.exit(1)
df = pd.concat(pezzi, ignore_index=True)   # indice unico: senza ignore_index le assegnazioni per indice colpiscono piu' strumenti

# ---------------------------------------------------------------- chiusura di oggi mancante
ultimo_giorno = df["date"].max()
manca = df[(df["date"] == ultimo_giorno) & (df["close"].isna()) & (df["open"].notna())]["yahoo"].unique().tolist()
ricavate = 0
for i in range(0, len(manca), 80):
    lista = manca[i:i + 80]
    try:
        m = yf.download(lista, period="1d", interval="1m", auto_adjust=False, group_by="ticker", threads=True, progress=False)
    except Exception as e:
        log.append(f"intraday blocco fallito: {e}"); continue
    for y in lista:
        try:
            c = (m[y] if len(lista) > 1 else m)["Close"].dropna()
        except KeyError:
            continue
        if c.empty:
            continue
        c = float(c.iloc[-1])
        idx = df[(df["yahoo"] == y) & (df["date"] == ultimo_giorno)].index
        df.loc[idx, "close"] = c
        df.loc[idx, "high"] = df.loc[idx, "high"].clip(lower=c)
        df.loc[idx, "low"] = df.loc[idx, "low"].clip(upper=c)
        ricavate += 1
log.append(f"chiusure del {ultimo_giorno} mancanti: {len(manca)}, ricavate dall'intraday: {ricavate}")
df = df.dropna(subset=["close"])

# ---------------------------------------------------------------- plausibilita' delle barre
anomale = ((df["high"] < df["low"]) | (df["high"] / df["low"] > 1.5)
           | (df["close"] > df["high"] * 1.1) | (df["close"] < df["low"] * 0.9)
           | (df["open"] > df["high"] * 1.1) | (df["open"] < df["low"] * 0.9))
anomale = anomale.fillna(False)
if anomale.any():
    log.append(f"barre scartate perche' implausibili: {int(anomale.sum())}: " + ", ".join(f"{r.yahoo} {r.date}" for r in df[anomale].head(15).itertuples()) + (" ..." if anomale.sum() > 15 else ""))
    df = df[~anomale]

# ---------------------------------------------------------------- output
meta = {s[4]: s for s in strumenti}
df["ticker"] = df["yahoo"].map(lambda y: meta[y][0])
df = df[["date", "ticker", "open", "high", "low", "close", "volume"]].sort_values(["ticker", "date"])
df.to_csv("data/prezzi.csv", index=False, float_format="%.4f")
presenti = set(df["ticker"])
with open("data/strumenti.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["ticker", "tipo", "isin", "nome", "yahoo", "mercato"])
    for s in strumenti:
        if s[0] in presenti:
            w.writerow(s)
log.append("strumenti con dati: %d su %d (etf promo %d, altri etf %d, azioni %d)" % (len(presenti), len(strumenti), *[sum(1 for s in strumenti if s[1] == k and s[0] in presenti) for k in ("etf_promo", "etf", "azione")]))
log.append("senza dati: " + ", ".join(s[0] for s in strumenti if s[0] not in presenti))
ult = df.groupby("ticker")["date"].max()
log.append(f"ultima data: {ult.max()}; strumenti non aggiornati a quella data: {(ult < ult.max()).sum()}")
open("data/log.txt", "w").write("\n".join(log) + "\n")
print("\n".join(log))
