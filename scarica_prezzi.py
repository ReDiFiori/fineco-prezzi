#!/usr/bin/env python3
"""Scarica da Yahoo Finance le barre giornaliere degli ETF dell'universo (Borsa Italiana, suffisso .MI)
e le salva in data/prezzi.csv (formato lungo: date,ticker,open,high,low,close,volume).
Se un ticker non restituisce dati prova a risolverlo dall'ISIN. Scrive un log in data/log.txt."""
import datetime as dt, json, os, sys
import pandas as pd
import yfinance as yf

UNIVERSO = {
    "SWDA": "IE00B4L5Y983", "XSPX": "LU0490618542", "A500": "LU1681048804", "XNAS": "IE00BMFKG444",
    "ANX": "LU1681038243", "XRS2": "IE00BJZ2DD79", "XESC": "LU0380865021", "XDAX": "LU0274211480",
    "XSX6": "LU0328475792", "XMEM": "LU0292107645", "XMJP": "LU0274209740", "XCS6": "LU0514695690",
    "CI2": "LU1681043086", "KOR": "LU1900066975", "BRA": "LU1900066207", "CHIP": "LU1900066033",
    "GOAI": "LU1861132840", "AINF": "IE000X59ZHE2", "XDWT": "IE00BM67HT60", "XDWH": "IE00BM67HK77",
    "XDW0": "IE00BM67HM91", "BNK": "LU1834983477", "TNO": "LU1834988518", "DEFS": "LU3038520774",
    "NRJC": "FR0014002CG3", "GOLD": "FR0013416716", "EM15": "LU1287023268", "XUTD": "LU0429459356",
}
os.makedirs("data", exist_ok=True)
mappa_file = "data/mappa_ticker.json"
mappa = json.load(open(mappa_file)) if os.path.exists(mappa_file) else {}
log = [f"run {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC"]

def scarica(sym):
    h = yf.Ticker(sym).history(period="2y", auto_adjust=False)
    if h is None or h.empty:
        return None
    h = h.reset_index()[["Date", "Open", "High", "Low", "Close", "Volume"]]
    h.columns = ["date", "open", "high", "low", "close", "volume"]
    h["date"] = pd.to_datetime(h["date"]).dt.date
    return h

def risolvi_da_isin(isin):
    try:
        q = yf.Search(isin, max_results=10).quotes
    except Exception as e:
        log.append(f"  ricerca ISIN {isin} fallita: {e}")
        return None
    for r in q:
        if r.get("exchange") in ("MIL", "ETF") or str(r.get("symbol", "")).endswith(".MI"):
            return r["symbol"]
    return q[0]["symbol"] if q else None

pezzi = []
for t, isin in UNIVERSO.items():
    sym = mappa.get(t, f"{t}.MI")
    h = scarica(sym)
    if h is None:
        alt = risolvi_da_isin(isin)
        if alt and alt != sym:
            h = scarica(alt)
            if h is not None:
                mappa[t] = alt
                log.append(f"  {t}: risolto da ISIN come {alt}")
    if h is None:
        log.append(f"  {t}: NESSUN DATO ({sym})")
        continue
    h.insert(1, "ticker", t)
    pezzi.append(h)
    log.append(f"  {t}: {len(h)} barre, ultima {h['date'].iloc[-1]} close {h['close'].iloc[-1]:.3f}")

if not pezzi:
    print("\n".join(log)); sys.exit(1)
df = pd.concat(pezzi).sort_values(["ticker", "date"])
df.to_csv("data/prezzi.csv", index=False, float_format="%.4f")
json.dump(mappa, open(mappa_file, "w"), indent=2)
open("data/log.txt", "w").write("\n".join(log) + "\n")
print("\n".join(log))
