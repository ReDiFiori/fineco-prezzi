#!/usr/bin/env python3
"""Versione 4 (tre livelli: etf_promo, etf, azione). Scarica da Yahoo Finance le barre giornaliere di:
  - tutti gli ETF della lista promo Fineco (promo_isin.csv: isin,nome), risolti su Borsa Italiana (.MI) dall'ISIN;
  - gli altri ETF in etf_extra.txt e le azioni in azioni.txt (ticker Yahoo;nome), Borsa Italiana e Xetra.
Scrive data/prezzi.csv (date,ticker,open,high,low,close,volume), data/strumenti.csv (ticker,tipo,isin,nome,yahoo)
e data/log.txt. La mappa ISIN -> simbolo Yahoo e' salvata in data/mappa_ticker.json e riusata.
Se Yahoo non ha ancora la chiusura ufficiale del giorno, la ricava dall'ultimo minuto scambiato."""
import csv, datetime as dt, json, os, sys, time
import pandas as pd
import yfinance as yf

os.makedirs("data", exist_ok=True)
MAPPA = "data/mappa_ticker.json"
mappa = json.load(open(MAPPA)) if os.path.exists(MAPPA) else {}
log = [f"run {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC"]

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

def risolvi_isin(isin):
    """Cerca su Yahoo il listing di Borsa Italiana (.MI) per l'ISIN. Ritorna (symbol, nome) o (None, None)."""
    for tentativo in range(2):
        try:
            q = yf.Search(isin, max_results=15).quotes
            break
        except Exception as e:
            q = []
            time.sleep(2)
    mi = [r for r in q if str(r.get("symbol", "")).endswith(".MI")]
    if mi:
        r = mi[0]
        return r["symbol"], r.get("longname") or r.get("shortname") or ""
    return None, None

# ---------------------------------------------------------------- risoluzione ETF
nuovi = 0
for isin, nome in etf.items():
    if isin in mappa:
        continue
    sym, nome_y = risolvi_isin(isin)
    mappa[isin] = {"yahoo": sym, "nome": nome_y or nome}
    nuovi += 1
    if nuovi % 25 == 0:
        json.dump(mappa, open(MAPPA, "w"), indent=1, ensure_ascii=False)
json.dump(mappa, open(MAPPA, "w"), indent=1, ensure_ascii=False)
log.append(f"ETF in lista: {len(etf)}, risolti su Borsa Italiana: {sum(1 for i in etf if mappa.get(i, {}).get('yahoo'))}, nuove risoluzioni: {nuovi}")

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
def blocco(lista):
    d = yf.download(lista, period="2y", interval="1d", auto_adjust=False, group_by="ticker", threads=True, progress=False)
    return d
for i in range(0, len(simboli), 80):
    lista = simboli[i:i + 80]
    try:
        d = blocco(lista)
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
df = pd.concat(pezzi)

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
