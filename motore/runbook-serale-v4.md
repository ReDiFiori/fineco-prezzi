# Runbook del segnale serale · versione 4 (motore v4, dal 9 settembre 2026)

Ogni sera alle 19:45 ora italiana, tutti i giorni, nella sessione pianificata "Segnale serale Fineco". Tutti i comandi in Bash nel container, in una cartella NUOVA creata al momento, per esempio `/home/claude/run-$(date +%H%M%S)`: mai cancellare file con `rm` (il comando `rm -f *` fa scattare una richiesta di permesso che nessuno può approvare e blocca la sessione per sempre). Lavora in autonomia: nessuno legge in tempo reale, non fare domande. Nei testi mai il trattino lungo.

## 0. Dove stanno le cose

- Repository pubblico GitHub `ReDiFiori/fineco-prezzi`, letto via `https://raw.githubusercontent.com/ReDiFiori/fineco-prezzi/main/...` con curl aggiungendo `?nocache=$(date +%s)` per evitare la cache:
  - `motore/segnali_v4.py` il motore (da salvare tale e quale, senza modificarlo)
  - `motore/strategia-v4.md` le regole, `motore/runbook-serale-v4.md` questo runbook
  - `data/prezzi.csv`, `data/strumenti.csv`, `data/log.txt` i dati (scritti dalle GitHub Actions alle 18:50 e 19:20 ora legale, 17:50 e 18:20 ora solare)
- Progetto claude.ai "Trading su Fineco" (strumento Projects): `claude/stato-portafoglio.json` (stato), `claude/telegram-config.md` (TG_TOKEN e TG_CHAT), `claude/registro.md` (operazioni chiuse), `claude/diario-segnali.md` (una riga per sera).

## 1. Preparazione

1. `date -u; TZ=Europe/Rome date`. Se è sabato, domenica o festività di Borsa Italiana (1 gennaio, Venerdì Santo, Lunedì dell'Angelo, 1 maggio, 15 agosto, 24, 25, 26 e 31 dicembre): esegui solo i passi 2, 3 (leggi_canale) e 6 (salvataggio), nessun segnale.
2. `R=/home/claude/run-$(date +%H%M%S); mkdir -p $R && cd $R` (cartella nuova, niente rm). Scarica il motore: `curl -sS -o segnali_v4.py "https://raw.githubusercontent.com/ReDiFiori/fineco-prezzi/main/motore/segnali_v4.py?nocache=$(date +%s)"`. Controlla: `head -3 segnali_v4.py` deve contenere "Motore dei segnali v4" e `python3 -m py_compile segnali_v4.py` deve passare. Se no, fermati e vai alla sezione Errori.
3. Con Projects leggi `claude/stato-portafoglio.json` e salvalo come `stato.json` (contenuto identico). Leggi `claude/telegram-config.md`: scrivi le due righe `TG_TOKEN=...` e `TG_CHAT=...` in un file `.env` con `chmod 600`, e caricale con `set -a; . ./.env; set +a` nello stesso comando Bash che usa il motore. Non scrivere mai i valori sulla riga di comando, nei messaggi, nel diario o nella memoria.
4. Scarica i dati: `data/log.txt`, `data/strumenti.csv`, `data/prezzi.csv` (stessa forma di URL con nocache). Freschezza: nel log la riga "ultima data: AAAA-MM-GG" deve essere la seduta di oggi (o l'ultima seduta di Borsa se oggi è festivo). Se i dati sono vecchi, aspetta 10 minuti e riprova una volta (`sleep 600`), poi sezione Errori.

## 2. Canale

`python3 segnali_v4.py leggi_canale --stato stato.json` legge i comandi scritti da Ugo nel canale nelle ultime 24 ore (ESEGUITO qta prezzo, NON ESEGUITO, VENDUTO qta prezzo, NON DISPONIBILE ticker, SALDO euro, PAUSA, RIPRENDI) e li applica allo stato. Gli esiti restano in `stato.json` (campo `eventi_pendenti`) e il motore li mette da solo nel messaggio serale. Leggi l'output: se un comando è "non capito", non interpretarlo tu: verrà segnalato nel messaggio.

## 3. Analisi

`python3 segnali_v4.py analizza --dati prezzi.csv --strumenti strumenti.csv --stato stato.json --out analisi.json`. Nei giorni festivi salta questo passo. Il motore:
- deduce gli eseguiti non confermati dai prezzi del giorno (con la regola del gap: se il titolo ha aperto sotto la soglia indicata la sera prima, l'ordine è considerato non eseguito);
- ricalcola stop e take profit sul prezzo eseguito, controlla stop e take profit sulle barre, alza lo stop mobile (chandelier 3 ATR dal massimo) dalla terza seduta, applica il time stop;
- se non c'è posizione né ordine, propone al massimo un acquisto per domani con: limite, soglia di gap sotto cui non comprare, percentuali di stop e take profit rispetto al prezzo eseguito e la tabella dei livelli per tre prezzi tipici.

## 4. Rilettura del messaggio

Leggi `analisi.json` (campo `messaggio`) con occhio critico:
- controlla che i prezzi citati siano coerenti con `prezzi.csv` (chiusura del ticker proposto, ultimo prezzo della posizione);
- controlla che la soglia di gap sia sotto la chiusura e che la tabella stop/take profit sia coerente con le percentuali;
- correggi SOLO errori evidenti di testo; non cambiare livelli, quantità o ticker: se ti sembrano sbagliati, non inviare istruzioni operative e vai alla sezione Errori;
- se la sessione parte in un orario diverso dalle 19:45 (avvio manuale), aggiungi una riga che chiarisce per quale seduta vale l'operazione.

## 5. Invio

Salva il messaggio in `messaggio.txt` e invia: `set -a; . ./.env; set +a; python3 segnali_v4.py invia --messaggio messaggio.txt`. La risposta deve contenere `"ok": true`.

## 6. Salvataggio

1. `claude/stato-portafoglio.json` sempre, con project_write e local_path `stato.json`.
2. `claude/registro.md`: una riga per ogni operazione chiusa oggi (campo `storico` dello stato: ingresso, uscita, ticker, quantità, prezzi, motivo, P/L netto).
3. `claude/diario-segnali.md`: aggiungi UNA riga con data, regime, equity, comandi letti, cosa è stato proposto o cosa è successo alla posizione, eventuali anomalie. Non riscrivere le righe precedenti.
4. Chiudi con un riepilogo di due righe: cosa hai inviato e cosa hai salvato.

## 7. Errori

- Regola generale: se un comando Bash chiede un permesso (rm, sudo, rete non consentita), non insistere: usa un'alternativa che non lo richieda (cartella nuova invece di cancellare, file diverso invece di sovrascrivere).
- Motore non scaricabile o non compilabile: non inviare segnali. Invia nel canale una riga: "Segnale serale non disponibile: motore non raggiungibile. Nessuna operazione per domani." Salva lo stato com'è e scrivi nel diario.
- Dati vecchi dopo il secondo tentativo: come sopra, con "dati non aggiornati". Se c'è una posizione aperta, ricorda nel messaggio che stop e take profit sul conto restano validi.
- Errore del motore in analizza: come sopra, con il testo dell'errore (una riga). Mai inventare prezzi o livelli, mai scrivere istruzioni operative non prodotte dal motore.
- Telegram irraggiungibile: riprova dopo 2 minuti; se fallisce ancora, salva comunque stato e diario e scrivi nel diario che il messaggio non è partito. Non perdere lo stato.
- Comandi nel canale contraddittori (per esempio ESEGUITO senza ordine pendente): il motore li segnala come esiti nel messaggio; non correggerli tu.
