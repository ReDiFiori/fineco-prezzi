#!/usr/bin/env python3
"""
bt5: test comparativo della regola di rotazione (proposta v5) sopra il motore v4, senza modificarlo.
Importa segnali_v4 tale e quale e cicla analizza seduta per seduta come bt4 (esecuzioni dedotte dai prezzi del giorno dopo).
La rotazione e' applicata dal harness DOPO analizza, con le stesse funzioni del motore (candidati, proposta, chiudi_posizione).

  python3 bt5.py --dati prezzi.csv --strumenti strumenti.csv [--fine 2026-09-08] [--out risultati.json]
"""
import argparse, json, sys, os, datetime as dt, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import segnali_v4 as m

def score_ticker(df, ticker, oggi, meta):
    """Punteggio del titolo in portafoglio con la stessa formula di candidati() (ranking 'vol')."""
    r = m.ultimo(df, ticker, oggi)
    if r is None: return None
    import math
    if any(math.isnan(r[k]) for k in ("r21", "r63", "atr")): return None
    score = 0.5 * float(r["r63"]) + 0.5 * float(r["r21"])
    base = score / max(float(r["atr_pct"]), 0.005)
    return base - m.P["penalita"].get(meta[ticker]["tipo"], 0.0)

def rotazione(df, meta, stato, oggi, res, cfg, log):
    """Se conviene, mette in stato una vendita pendente della posizione e un ordine sul candidato migliore."""
    if not cfg["on"] or not stato["posizioni"] or stato["ordini_pendenti"] or stato["uscite_pendenti"] or not stato["attivo"]: return
    p = stato["posizioni"][0]
    if p.get("sedute", 0) < cfg["sedute_min"]: return
    if m.ingressi_settimana(stato, oggi) >= m.P["max_ingressi_settimana"]: return
    s_held = score_ticker(df, p["ticker"], oggi, meta)
    if s_held is None: return
    c = float(p["ultimo"]); limite_v = m.tick(c * (1 - m.P["limite_uscita"]))
    pl = p["qta"] * (c - p["prezzo"]) - m.P["commissione"] - p["costo_apertura"]      # = pl_aperto
    for cnd in res["classifica_completa"]:
        if not cnd["ok"]: continue
        if cnd["score_adj"] < cfg["margine"] * max(s_held, 1e-9): return
        if s_held <= 0 and cnd["score_adj"] <= 0: return
        # cash stimato dopo la vendita al limite: con quello si dimensiona il nuovo ordine
        tmp = copy.deepcopy(stato); tmp["cash"] = stato["cash"] + p["qta"] * limite_v - m.P["commissione"]; tmp["posizioni"] = []
        eq = tmp["cash"]
        pr = m.proposta(tmp, cnd, eq)
        if not pr: continue
        costo_nuovo = m.costo_acquisto(cnd["tipo"], cnd["mercato"], pr["controvalore"])
        if pl <= 0 or pl < costo_nuovo + cfg["pl_extra"]: return
        stato["uscite_pendenti"].append(dict(ticker=p["ticker"], qta=p["qta"], limite=limite_v, motivo="rotazione", data_segnale=oggi.isoformat()))
        stato["ordini_pendenti"].append({**pr, "data_segnale": oggi.isoformat(), "rotazione_da": p["ticker"]})
        log.append(dict(data=oggi.isoformat(), da=p["ticker"], a=cnd["ticker"], pl_atteso=round(pl, 2), score_da=round(s_held, 2), score_a=round(cnd["score_adj"], 2), sedute=p.get("sedute")))
        return

def run(df, meta, date, cfg):
    stato = json.loads(json.dumps(m.STATO_INIZIALE))
    curva = []; log = []; ricavi = []
    for oggi in date:
        rot_orders = {o["ticker"]: o["rotazione_da"] for o in stato["ordini_pendenti"] if o.get("rotazione_da")}
        res = m.analizza(df, meta, stato, oggi)
        for p in stato["posizioni"]:
            if p["ticker"] in rot_orders and "rotazione_da" not in p: p["rotazione_da"] = rot_orders[p["ticker"]]
        # classifica completa (analizza tronca a 10): la ricalcolo con le funzioni del motore
        res["classifica_completa"] = m.candidati(df, meta, oggi, stato, res["regime"]) if cfg["on"] and stato["posizioni"] else []
        rotazione(df, meta, stato, oggi, res, cfg, log)
        curva.append((oggi.isoformat(), res["equity"]))
    # chiusura contabile: posizioni aperte valutate all'ultimo prezzo
    eq_fin = m.equity(stato, df, date[-1])
    picco = -1; dd = 0
    for _, e in curva:
        picco = max(picco, e); dd = max(dd, (picco - e) / picco)
    st = stato["storico"]
    costi = sum(x["costo_totale"] for x in st)
    rot = [x for x in st if x["motivo"] == "rotazione"]
    da_rot = [x for x in st if x.get("rotazione_da")]
    return dict(equity_finale=round(eq_fin, 2), rendimento_pct=round((eq_fin / 500 - 1) * 100, 1), discesa_max_pct=round(dd * 100, 1),
                operazioni=len(st), vincenti=sum(1 for x in st if x["pl_netto"] > 0), costi=round(costi, 2),
                rotazioni_segnalate=len(log), vendite_per_rotazione=len(rot), pl_vendite_rotazione=round(sum(x["pl_netto"] for x in rot), 2),
                posizioni_aperte_da_rotazione=len(da_rot), pl_posizioni_da_rotazione=round(sum(x["pl_netto"] for x in da_rot), 2),
                aperte_fine=[(p["ticker"], p["data"]) for p in stato["posizioni"]], log_rotazioni=log, storico=st, curva=curva)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dati", required=True); ap.add_argument("--strumenti", required=True)
    ap.add_argument("--fine"); ap.add_argument("--inizio", default="2025-07-09"); ap.add_argument("--out")
    ap.add_argument("--solo", help="nome di una sola configurazione")
    a = ap.parse_args()
    df, meta = m.carica(a.dati, a.strumenti)
    date = sorted(d for d in df[df["ticker"] == m.TICKER_REGIME]["date"].unique() if d >= dt.date.fromisoformat(a.inizio) and (not a.fine or d <= dt.date.fromisoformat(a.fine)))
    print(f"sedute: {len(date)} dal {date[0]} al {date[-1]}", file=sys.stderr)
    configs = {
        "v4 (riferimento, rotazione spenta)": dict(on=False, margine=2, sedute_min=10, pl_extra=0),
        "v5 Ugo: pl >= costo nuovo ingresso, margine 2, sedute min 10": dict(on=True, margine=2.0, sedute_min=10, pl_extra=0),
        "v5 margine 1,5, sedute 10": dict(on=True, margine=1.5, sedute_min=10, pl_extra=0),
        "v5 margine 3, sedute 10": dict(on=True, margine=3.0, sedute_min=10, pl_extra=0),
        "v5 margine 2, sedute 5": dict(on=True, margine=2.0, sedute_min=5, pl_extra=0),
        "v5 margine 2, sedute 15": dict(on=True, margine=2.0, sedute_min=15, pl_extra=0),
        "v5 margine 2, sedute 10, pl >= costo + 5 euro": dict(on=True, margine=2.0, sedute_min=10, pl_extra=5.0),
    }
    out = {}
    for nome, cfg in configs.items():
        if a.solo and a.solo not in nome: continue
        r = run(df, meta, date, cfg); out[nome] = r
        print(f"{nome:62} | {r['equity_finale']:8.2f} | {r['rendimento_pct']:+6.1f}% | dd {r['discesa_max_pct']:5.1f}% | op {r['operazioni']:2d} ({r['vincenti']:2d}) | costi {r['costi']:6.2f} | rot segn {r['rotazioni_segnalate']:2d} vend {r['vendite_per_rotazione']:2d} pl {r['pl_vendite_rotazione']:+7.2f} | nuove {r['posizioni_aperte_da_rotazione']:2d} pl {r['pl_posizioni_da_rotazione']:+7.2f}")
    if a.out: json.dump(out, open(a.out, "w"), indent=1, ensure_ascii=False, default=str)

if __name__ == "__main__":
    main()
