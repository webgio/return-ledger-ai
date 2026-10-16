# Return Ledger — importazione AI da nota informativa PDF

`POST /api/extract` riceve un PDF (nota di acquisto/vendita, avviso di stacco cedola o
dividendo, oppure rendiconto periodico dei titoli, di banca o broker) e restituisce una riga
proposta per il registro. L'utente la conferma nell'app: l'AI propone, non scrive.

Un rendiconto diventa una riga `valuation`: in anteprima l'utente sceglie se usare il saldo
totale (registro che segue tutto il deposito) o il controvalore di un solo titolo (registro per
strumento). Controlli: le posizioni sommano al totale stampato, quantità × prezzo (+ rateo)
torna con ogni posizione, ogni cifra è nel testo del PDF.

Mappatura: acquisto = `deposit`; vendita, cedola, dividendo = `withdraw` (denaro che esce dal
portafoglio). `amount` è il lordo; commissioni e imposte nelle loro colonne, con il segno del
costo per l'investitore: un credito d'imposta (es. imposta sul rateo pagato comprando un BTP)
è una `tax` negativa. Il modello trascrive il segno stampato accanto a ogni voce (+/-).

## Struttura

    api/core/models.py     schema estratto (il modello trascrive le voci, non fa somme)
    api/core/extract.py    PDF -> chiamata al modello (tool call forzata) -> validazione -> 1 retry
    api/core/validate.py   controlli deterministici: aritmetica, coerenza, "il numero è nel PDF?"
    api/core/ledger.py     estrazione -> riga (amount lordo, fees, tax, data valuta, note)
    api/core/gate.py       chi chiama (codice ospite; login SWA pronto ma spento) + quote
    api/core/store.py      MemoryStore (test) / TableStore (Azure Table Storage)
    api/function_app.py    handler HTTP, sottile; la logica sta in core/ e non dipende da Azure
    scripts/guests_admin.py  add / list / revoke / enable / quota / reset ospiti
    app/staticwebapp.config.json  runtime API (python:3.11), header di sicurezza

## Provarlo in locale

Il modo più rapido (pagina + API sulla stessa origine, store in memoria, niente Azurite):

    export ANTHROPIC_API_KEY=$(security find-generic-password -s anthropic-return-ledger -w)
    api/.venv/bin/python scripts/dev_server.py          # http://127.0.0.1:8000/, codice "demo"

Con il runtime vero delle Functions:

    cd api && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
    cp local.settings.json.example local.settings.json   # metti ANTHROPIC_API_KEY
    npx azurite-table &                                  # emulatore Table Storage
    func start                                           # Azure Functions Core Tools
    export AZURE_TABLES_CONNECTION_STRING="UseDevelopmentStorage=true"
    python ../scripts/guests_admin.py add mario --quota 20   # stampa il codice, una volta sola
    curl -X POST http://localhost:7071/api/extract -H "x-guest-code: <codice>" \
         -H "Content-Type: application/pdf" --data-binary @nota.pdf

Test: `pip install pytest && pytest` (dal root). Quelli su Table Storage partono solo se Azurite
è attivo; quelli sul PDF reale cercano il file in `MPS_PDF` e altrimenti si saltano.

## Dal frontend

In `app/index.html` è già integrato: bottone "Add transaction from PDF" nella card del registro, campo
codice (salvato in localStorage solo se l'utente lo lascia spuntato, e lo dice), avviso privacy,
anteprima modificabile della riga con errori in rosso e avvisi in ambra; con `needs_review`
il bottone "Add to this ledger" resta disattivato finché l'utente non spunta "I checked these
figures". La riga entra solo con `addRow()` al clic. Se l'API manca (pagina aperta da disco o
hosting solo statico) il resto della pagina funziona e il pannello lo dice. Un codice rifiutato
(401) viene dimenticato.

Il contratto:

    const r = await fetch("/api/extract", {
      method: "POST",
      headers: { "x-guest-code": codice, "Content-Type": "application/pdf" },
      body: file,                       // File dall'<input type=file>
    });
    const data = await r.json();
    if (!r.ok) { /* data.error = { code, message } */ }
    const { status, row, errors, warnings, remaining } = data;
    // status "needs_review": mostra errors/warnings in evidenza e lascia modificare la riga

I testi per l'utente sono in inglese (prima lingua della UI). Ogni errore e avviso ha un `code`
stabile più un `message` inglese: la futura versione italiana traduce nel frontend partendo dal
codice. Fanno eccezione gli avvisi `model_note`, testo libero del modello (in inglese).

Risposte di errore: `{"error": {"code", "message"}}`, più `remaining` dove c'è.

| HTTP | code | Note |
|---|---|---|
| 200 | — | `kind` `trade` (con `row`, `details`) o `statement` (con `valuation`: `{date, options: [{label, value, note}]}`, la prima opzione è il saldo totale); `status` `ok` / `needs_review`; `errors`/`warnings` = liste di `{code, message}` |
| 401 | `code_required`, `invalid_code`, `invalid_identity` | |
| 413 | `file_too_large`, `too_many_pages` | non consuma quota |
| 415 | `not_pdf` | non consuma quota |
| 422 | `pdf_encrypted`, `pdf_unreadable` | non consuma quota |
| 422 | `unreadable_document`, `not_a_trade`, `multi_fill_detail` | quota consumata (il modello è stato chiamato) |
| 429 | `too_many_attempts`, `quota_exhausted` | |
| 502 | `model_unavailable` | quota restituita |
| 503 | `ai_disabled`, `daily_cap` | |

Codici in `errors`: `net_mismatch`, `figure_not_in_document`, `missing_date`,
`settlement_before_trade`, `not_a_trade`, `multi_fill_detail`, `statement_total_mismatch`,
`no_positions`, `bad_model_output`, `extraction_failed`. `multi_fill_detail` = dettaglio dei singoli eseguiti di un ordine
multieseguito: va importato il riepilogo, altrimenti l'operazione entrerebbe due volte.
Codici in `warnings`: `invalid_isin`, `quantity_price_mismatch`, `position_value_mismatch`,
`non_eur_currency`, `no_text_layer`, `model_note`.

## Ospiti e crediti

    export AZURE_TABLES_CONNECTION_STRING="..."    # in locale: UseDevelopmentStorage=true (Azurite)
    python scripts/guests_admin.py add mario --quota 20   # stampa il codice, una volta sola
    python scripts/guests_admin.py list                   # consumo del mese per ospite
    python scripts/guests_admin.py quota mario 50         # nuova quota mensile, vale da subito
    python scripts/guests_admin.py reset mario            # azzera il consumo di questo mese
    python scripts/guests_admin.py revoke mario           # (enable per riattivare)

La quota è mensile e si azzera da sola il primo del mese (UTC). Il codice non si può rileggere
(è salvato solo l'hash): se un ospite lo perde, `revoke` e `add` con un nuovo nome. Oltre alle
quote individuali c'è il tetto giornaliero globale `GLOBAL_DAILY_CAP`. Il dev server locale non
usa Table Storage: un solo ospite, `--code` e `--quota`, e i contatori si azzerano al riavvio.

## Impostazioni (app settings della SWA)

| Nome | Note |
|---|---|
| ANTHROPIC_API_KEY | chiave di un workspace dedicato, con tetto di spesa |
| ANTHROPIC_MODEL | default claude-haiku-5-5; claude-haiku-4-5-20251001 è stato provato e funziona anch'esso |
| AZURE_TABLES_CONNECTION_STRING | account Storage per le tabelle Guests e Usage |
| AI_ENABLED | `false` = interruttore di emergenza |
| GLOBAL_DAILY_CAP | chiamate al giorno in totale (default 200) |
| MAX_FAILED_PER_HOUR | tentativi con codice errato per IP (default 20) |
| MODEL_TIMEOUT_S | default 18: 2 tentativi stanno nel timeout di ~45 s delle managed functions |
| ALLOW_LOGIN / DEFAULT_USER_QUOTA | per il pubblico: abilita il login SWA (default spento) |

## Cosa è verificato e cosa no

Verificato: logica di estrazione/validazione sul PDF MPS reale (riproduce la riga già
inserita a mano), quote e ospiti anche in parallelo su Azurite, handler HTTP, forma della
richiesta che l'SDK Anthropic invia, chiamata vera al modello sul PDF MPS (Haiku 5.5 e 4.5:
tutto corretto al primo tentativo, 2,5–4,8 s, nessun avviso).
Batteria di prove su documenti veri: `python scripts/eval_fixtures.py <cartella>` (PDF +
`expected.json`, fuori da git; la mia è `~/Documents/return-ledger-fixtures`, 43 documenti MPS:
acquisti, vendite, BTP con rateo, cedole, dividendi, multieseguito, rendiconti, informative).
Prova manuale: `python scripts/try_extract.py nota.pdf [--model ...]` con `ANTHROPIC_API_KEY`
nell'ambiente (in locale: portachiavi macOS, `security find-generic-password -s anthropic-return-ledger -w`).
Non verificato: altri documenti oltre al PDF MPS, il deploy su SWA e la versione di Python
supportata dalle managed functions in questo momento.

## Privacy

Il PDF contiene nomi, indirizzo e numeri di conto e viene inviato al fornitore del modello.
Non viene salvato né loggato; le tabelle contengono solo hash dei codici e contatori.
Va dichiarato nell'informativa prima di aprire al pubblico.
