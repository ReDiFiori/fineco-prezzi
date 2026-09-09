# Strategia Fineco v4: rotazione momentum su ETF e azioni, con stop di struttura e ricalcolo sul prezzo eseguito

Versione 4, 9 settembre 2026, PROPOSTA in attesa del via libera di Ugo. Nasce dalla prima operazione reale (BNK, 9/9: ingresso in gap negativo, stop toccato nella stessa seduta) e dal confronto con le pratiche consolidate del trend following e del momentum su ETF. Riprende dalla v3 tutto quello che non è citato qui: universo a tre livelli, costi Fineco, regime SWDA sopra/sotto media 200, filtri di trend, liquidità e storia, una posizione alla volta, massimo tre ingressi a settimana, rischio 7% per operazione (3,5% sotto i 425 €), kill switch a 350 €.

Motore: `motore/segnali v4.py`. Con i parametri v4 spenti riproduce la v3 al centesimo (test bt4, configurazione v3eq: 585,60 €, 21 operazioni). Test comparativo: `motore/bt4.py`, risultati in `motore/bt4_risultati.json`.

## 1. Cosa cambia rispetto alla v3, e perché

1. Stop e take profit ricalcolati sul prezzo eseguito. La v3 li calcolava sulla chiusura del giorno prima e li lasciava lì anche se l'ingresso avveniva a un prezzo diverso: con BNK il prezzo di ingresso era 1,6% sopra lo stop invece del 2,5% previsto. La v4 conserva le distanze percentuali del segnale e le applica al prezzo reale (comando ESEGUITO, oppure prezzo dedotto).
2. Regola del gap in apertura. Se il titolo apre sotto la chiusura meno mezzo ATR, il segnale non vale più e non si compra: un gap contro la direzione del segnale è la prima cosa che i sistemi di momentum scartano (Clenow esclude anche chi ha avuto gap superiori al 15% negli ultimi 90 giorni, regola ripresa qui). Il messaggio serale scrive esplicitamente "non comprare se in apertura il prezzo è sotto X".
3. Stop iniziale di struttura. Lo stop va sotto il minimo delle ultime 10 sedute meno mezzo ATR, se questo livello è più lontano dello stop a 2 ATR, sempre entro il 6%. È la pratica standard degli swing trader: lo stop deve stare dove il ragionamento è sbagliato, cioè sotto il supporto, con un cuscinetto di volatilità che sopravvive alle "spazzate" del minimo; uno stop a distanza fissa dal prezzo viene colpito dal rumore. Con BNK avrebbe dato 72,69 invece di 73,79: il minimo del 9/9 (73,73) non lo avrebbe toccato.
4. Stop mobile alla chandelier. Dalla terza seduta lo stop sale al massimo raggiunto dall'ingresso meno 3 ATR (Le Beau), non più alla chiusura meno 2 ATR: segue il trend senza inseguire ogni oscillazione. Il pareggio più costi a +1R resta.
5. Take profit facoltativo. I sistemi di trend following non usano target fissi (Clenow, Faber, Antonacci escono solo quando il trend finisce), e le prove pubblicate danno alla chandelier la migliore aspettativa per operazione e la discesa minima. Nel nostro caso il take profit ha però un vantaggio operativo: è un ordine che lavora da solo mentre Ugo non guarda. Per questo resta un parametro (usa_tp) e la decisione la prendono i numeri della sezione 4.
6. Classifica alternativa "Clenow": pendenza della regressione esponenziale a 90 giorni, annualizzata, moltiplicata per R quadro. Premia i trend regolari e penalizza quelli fatti di salti. Parametro ranking="clenow", da confrontare con la classifica v3 (momentum diviso ATR).
7. Gli esiti dei comandi del canale (ESEGUITO, VENDUTO, SALDO) compaiono nel messaggio serale, così Ugo vede che il motore li ha letti e con quali livelli.

## 2. Protocollo di comunicazione (il problema dei tempi)

Il motore legge il canale una volta al giorno, alle 19:45. Ugo compra in apertura e ha bisogno di stop e take profit subito, non la sera. La soluzione non è una seconda routine ma togliere il bisogno della risposta:

- Il messaggio serale porta già tutto: la soglia di gap sotto cui non comprare, le percentuali di stop e take profit rispetto al prezzo eseguito, e una tabella con i livelli per tre prezzi tipici (limite, chiusura, soglia di gap). Ugo compra, legge il prezzo eseguito, prende dalla tabella la riga più vicina (o applica le percentuali) e inserisce subito stop e take profit. Nessuna attesa.
- Ugo scrive nel canale ESEGUITO qta prezzo appena può (vale 24 ore). La sera il motore ricalcola i livelli esatti sul prezzo scritto e li conferma nel messaggio: se differiscono di più di un tick da quelli inseriti, Ugo li allinea.
- Se Ugo non scrive, il motore deduce l'eseguito dai prezzi (minimo tra apertura e limite) e ricalcola su quello, dichiarandolo.
- Se serve una risposta più rapida in casi particolari, resta possibile lanciare la routine a mano da Claude Code (RemoteTrigger run) o dalla pagina delle routine: costa una sessione cloud e 4 minuti. Una routine fissa di metà giornata non è consigliata: raddoppia i costi per un beneficio che la tabella già copre.

Comandi del canale: invariati (ESEGUITO, NON ESEGUITO, VENDUTO/CHIUSO, NON DISPONIBILE, SALDO, PAUSA, RIPRENDI).

## 3. Fonti consultate e cosa si applica al nostro caso

- Clenow, "Stocks on the Move": classifica per pendenza esponenziale a 90 giorni per R quadro, esclusione dei titoli con gap oltre il 15%, ingressi solo con l'indice sopra la media a 200, dimensione per ATR (obiettivo 10 punti base al giorno). Applicato: filtro gap storico, classifica alternativa, regime già presente. Non applicato: portafoglio di 20-30 titoli ribilanciato ogni due settimane (impossibile con 500 €). [QuantConnect: Stocks on the Move](https://www.quantconnect.com/forum/discussion/16578/quot-stocks-on-the-move-quot-momentum-strategy-by-cabedovestment/), [Stock_EXPOREG](https://github.com/rawsashimi1604/Stock_EXPOREG), [How (Not) to Invest: Clenow Momentum](https://chrischow.github.io/dataandstuff/2018-11-10-how-not-to-invest-clenow-momentum/)
- Chandelier exit (Le Beau) e stop ATR: lo stop mobile ancorato al massimo dall'ingresso meno un multiplo di ATR (2,5-3 per posizioni di più giorni) dà nei test pubblicati la migliore aspettativa per operazione e la discesa minima rispetto a stop fissi e percentuali. Applicato: trailing 3 ATR dal massimo. [LuxAlgo: ATR stop strategies](https://www.luxalgo.com/blog/5-atr-stop-loss-strategies-for-risk-control/), [CFI: Chandelier Exit](https://corporatefinanceinstitute.com/resources/equities/chandelier-exit/), [Volatility Box: volatility-adjusted stops](https://volatilitybox.com/research/volatility-adjusted-stop-losses/)
- Stop di struttura: lo stop un mezzo ATR sotto il minimo di riferimento (swing low) rispetta il livello che invalida l'idea e sopravvive alle spazzate; multipli 2-3 ATR per lo swing trading, 3-4 per il trend following. Applicato: minimo a 10 sedute meno 0,5 ATR, entro il 6%. [Traders Second Brain: ATR vs structure](https://traderssecondbrain.com/guides/stop-loss-placement-methods), [Chart Whisperer: ATR stop guide](https://chartwhisperer.ca/blog/atr-volatility-stop-loss-trading-guide)
- Antonacci, dual momentum (GEM): momentum relativo a 12 mesi più filtro assoluto (rendimento sopra i T-bill, altrimenti obbligazioni), una decisione al mese. Applicato in parte: il nostro regime SWDA sopra media 200 con rifugio in oro e governativi è la stessa idea. Non applicato: orizzonte a 12 mesi e rotazione mensile tra tre ETF, troppo lenta per l'obiettivo di Ugo. [Robot Wealth: Dual Momentum review](https://robotwealth.com/dual-momentum-review/), [QuantifiedStrategies: Dual Momentum](https://www.quantifiedstrategies.com/dual-momentum-trading-strategy/)

Nessuna di queste fonti risolve il limite vero della nostra situazione: con 500 € e una posizione alla volta, i costi fissi di Fineco (2,95 € a lato, cioè 0,6% ogni operazione completa sugli ETF promo) e l'assenza di diversificazione pesano più della finezza delle regole. Le regole servono a non buttare via il capitale in stop inutili, non a trasformare 500 € in 650 in quattro mesi.

## 4. Risultati del test comparativo (dati reali, 509 strumenti, 294 sedute dal 9 luglio 2025 all'8 settembre 2026)

Stessa meccanica di bt3: si parte da 500 €, si cicla il motore seduta per seduta, le esecuzioni sono dedotte dai prezzi del giorno dopo. La riga v3 è la strategia in vigore riprodotta dal motore v4 con le novità spente.

| Configurazione | Equity finale | Rendimento | Discesa max | Operazioni (vincenti) | Costi | Note |
|---|---|---|---|---|---|---|
| v3 (in vigore) | 585,60 € | +17,1% | -6,6% | 21 (12) | 71,86 € | riferimento |
| + ricalcolo su eseguito | 621,91 € | +24,4% | -8,8% | 25 (13) | 87,37 € | |
| + ricalcolo + regola del gap (0,5 ATR) | 650,60 € | +30,1% | -7,3% | 23 (13) | 81,55 € | identico con soglia a 1 ATR |
| + ricalcolo + gap + chandelier 3 ATR = **v4 proposta** | **689,81 €** | **+38,0%** | **-9,1%** | 20 (12) | 76,33 € | promo 15 op +74,7; azioni 4 op +67,3; altri ETF 1 op +56,5 |
| v4 con chandelier 2,5 ATR | 702,52 € | +40,5% | -8,8% | 24 (14) | 91,06 € | |
| v4 con chandelier 3,5 ATR | 526,19 € | +5,2% | -13,7% | 22 (9) | 74,22 € | sensibile al multiplo: vedi lettura |
| v4 + stop di struttura | 626,56 € | +25,3% | -8,2% | 18 (12) | 56,62 € | meno operazioni, meno costi |
| v4 + filtro gap storico 15% | 580,45 € | +16,1% | -23,5% | 25 (13) | 91,45 € | peggiora la discesa |
| v4 senza take profit (solo stop mobile) | 628,72 € | +25,7% | -7,8% | 13 (9) | 51,96 € | metà operazioni, discesa minima |
| v4 solo ETF (niente azioni) | 631,97 € | +26,4% | -9,5% | 20 (12) | 64,90 € | |
| ricalcolo + gap + stop di struttura (senza chandelier) | 528,26 € | +5,7% | -14,5% | 21 (13) | 72,40 € | |
| v4 con classifica Clenow | 417,28 € | -16,5% | -33,7% | 22 (8) | 111,73 € | seleziona azioni, perde |
| v4 con take profit 3R | 532,52 € | +6,5% | -16,6% | 16 (10) | 57,70 € | |
| v4 con stop iniziale 3 ATR | 502,48 € | +0,5% | -10,7% | 13 (8) | 41,30 € | |

## 5. Lettura, con franchezza

1. Le due correzioni nate dal caso BNK (ricalcolo sul prezzo eseguito e regola del gap) migliorano il risultato in ogni combinazione provata e correggono un difetto reale del motore, non un'ipotesi statistica: sono da adottare. Sul caso concreto del 9/9: la regola del gap avrebbe detto "non comprare sotto 75,25" (apertura 74,97), e anche comprando lo stop ricalcolato sarebbe stato 73,10, non toccato dal minimo di 73,73. Questo è senno di poi, e vale come esempio, non come prova.
2. Lo stop mobile alla chandelier a 3 ATR dal massimo è coerente con la letteratura e migliora il campione (+38% contro +30%), ma la sensibilità al multiplo (2,5: +40%; 3: +38%; 3,5: +5%) dice che le differenze sono fatte da poche operazioni. Lo adotto perché è la pratica consolidata, non per i numeri.
3. Lo stop di struttura e il filtro sui gap storici, che in teoria dovrebbero aiutare, peggiorano qui: il primo allarga gli stop e quindi le perdite, il secondo toglie proprio gli ETF che avevano appena fatto un salto e continuavano a salire. Restano nel motore come opzioni spente.
4. La classifica Clenow non si applica: sceglie azioni e ETF a regressione "pulita" ma con ATR alto, perde il 16% e la discesa arriva al 34%. La classifica v3 (momentum diviso ATR) resta.
5. Senza take profit si fanno metà operazioni con costi dimezzati e discesa minima (7,8%), rendimento +25,7%. È l'alternativa più "da trend follower" e vale la pena riprovarla tra qualche mese con più dati: per ora il take profit resta perché lavora da solo sul conto mentre Ugo non guarda.
6. Il campione è di 20 operazioni su 14 mesi. Il +38% non è una previsione: l'obiettivo dichiarato resta 15-25% l'anno, con discese del 10-15% da mettere in conto.

## 6. Decisione proposta

Adottare la v4 con: ricalcolo sul prezzo eseguito, regola del gap a 0,5 ATR, stop iniziale 2 ATR (minimo 2,5%, massimo 6%), chandelier 3 ATR dal massimo dalla terza seduta, take profit 2R (tetto 15%), classifica v3, tre livelli di strumenti invariati. Stop di struttura, filtro gap storico e classifica Clenow restano disponibili ma spenti.

## 7. Messa in produzione (dopo il via libera)

1. Nel progetto claude.ai "Trading su Fineco": salvare `claude/strategia-v4.md` (questo file) e `claude/motore-segnali.py.md` con il codice di `segnali v4.py`; aggiornare `claude/runbook-serale.md` (nome del file segnali_v4.py, lettura degli esiti dei comandi nel messaggio, tabella stop/take profit). Lo stato attuale è compatibile (il motore aggiunge `eventi_pendenti` da solo).
2. Aggiornare il prompt della routine "Segnale serale Fineco" (RemoteTrigger update) dove cita la v3 e segnali_v3.py.
3. Il primo segnale v4 va letto con attenzione: controllare che la tabella stop/take profit e la soglia di gap siano coerenti con i prezzi.

