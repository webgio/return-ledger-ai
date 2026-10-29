# Return Ledger — AI import from PDF trade notices

Return Ledger is a free, client-side portfolio performance tracker (TWR and XIRR, gross and net of
fees and tax): one self-contained page in `app/`, no accounts, data kept in the browser's
localStorage. This repository also holds an optional AI import feature, described below.

`POST /api/extract` receives a PDF (a purchase/sale confirmation, a coupon or dividend notice, or a
periodic securities statement from a bank or broker) and returns a proposed ledger row. The user
confirms it in the app: the AI proposes, it never writes.

A statement becomes a `valuation` row: in the preview the user chooses between the account total
(a ledger that tracks the whole deposit) and the value of a single position (a per-instrument
ledger). Checks: the positions add up to the printed total, quantity × price (+ accrual) matches
each position, and every figure appears in the PDF text.

Mapping: purchase = `deposit`; sale, coupon, dividend = `withdraw` (cash leaving the portfolio).
`amount` is the gross flow; fees and taxes go in their own columns, carrying the sign of the cost
to the investor: a tax credit (e.g. substitute tax on accrued interest paid when buying a bond) is
a negative `tax`. The model transcribes the sign printed next to each line (+/-).

The model only transcribes printed figures. All arithmetic and checks are done in code; if they
fail, the extraction is retried once with the errors fed back, and the result is `ok` or
`needs_review`.

## Layout

    api/core/models.py     extraction schema (the model transcribes lines, it does no arithmetic)
    api/core/extract.py    prompt, tool schemas, extract -> validate -> retry-once loop (model-free)
    api/core/extract_lc.py the single engine: calls the model through LangChain (forced tool call,
                           PDF as a file block)
    api/core/validate.py   deterministic checks: arithmetic, consistency, "is the figure in the PDF?"
    api/core/ledger.py     extraction -> row (gross amount, fees, tax, value date, note)
    api/core/gate.py       who is calling (guest code; SWA login prepared but off) + quotas
    api/core/store.py      MemoryStore (tests) / TableStore (Azure Table Storage)
    api/function_app.py    thin HTTP handler; the logic lives in core/ and has no Azure dependency
    scripts/guests_admin.py  add / list / revoke / enable / quota / reset / rotate guests
    app/staticwebapp.config.json  API runtime (python:3.11), security headers

## Running it locally

The quickest way (page and API on the same origin, in-memory store, no Azurite):

    export ANTHROPIC_API_KEY=...   # never commit it
    api/.venv/bin/python scripts/dev_server.py          # http://127.0.0.1:8000/, code "demo"

With the real Functions runtime:

    cd api && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
    cp local.settings.json.example local.settings.json   # set ANTHROPIC_API_KEY
    npx azurite-table &                                  # Table Storage emulator
    func start                                           # Azure Functions Core Tools
    export AZURE_TABLES_CONNECTION_STRING="UseDevelopmentStorage=true"
    python ../scripts/guests_admin.py add mario --quota 20   # prints the code, once
    curl -X POST http://localhost:7071/api/extract -H "x-guest-code: <code>" \
         -H "Content-Type: application/pdf" --data-binary @notice.pdf

Tests: `pip install pytest && pytest` (from the repo root). The Table Storage tests only run when
Azurite is up. The tests on a real PDF use the anonymized MPS notice in `tests/fixtures/`; set
`MPS_PDF` to use another file.

## From the frontend

Already wired into `app/index.html`: an "Add transaction from PDF" button in the ledger card, an
access-code field (kept in localStorage only if the user leaves the box ticked, and it says so), a
privacy notice, and an editable preview of the row with errors in red and warnings in amber. With
`needs_review` the "Add to this ledger" button stays disabled until the user ticks "I checked these
figures". The row only enters the ledger through `addRow()` on click. If the API is missing (page
opened from disk or static-only hosting) the rest of the page works and the panel says so. A
rejected code (401) is forgotten.

The contract:

    const r = await fetch("/api/extract", {
      method: "POST",
      headers: { "x-guest-code": code, "Content-Type": "application/pdf" },
      body: file,                       // File from the <input type=file>
    });
    const data = await r.json();
    if (!r.ok) { /* data.error = { code, message } */ }
    const { status, row, errors, warnings, remaining } = data;
    // status "needs_review": show errors/warnings prominently and let the user edit the row

User-facing strings are in English. Every error and warning carries a stable `code` plus an English
`message`, so a translated frontend can work from the code. The exception is `model_note` warnings,
free text from the model (in English; capped at 3 notes of 120 characters).

Error responses: `{"error": {"code", "message"}}`, plus `remaining` where relevant.

| HTTP | code | Notes |
|---|---|---|
| 200 | — | `kind` is `trade` (with `row`, `details`) or `statement` (with `valuation`: `{date, options: [{label, value, note}]}`, the first option is the account total); `status` is `ok` / `needs_review`; `errors`/`warnings` are lists of `{code, message}` |
| 401 | `code_required`, `invalid_code`, `invalid_identity` | |
| 413 | `file_too_large`, `too_many_pages` | does not use quota |
| 415 | `not_pdf` | does not use quota |
| 422 | `pdf_encrypted`, `pdf_unreadable` | does not use quota |
| 422 | `unreadable_document`, `not_a_trade`, `multi_fill_detail` | quota used (the model was called) |
| 429 | `too_many_attempts`, `quota_exhausted` | |
| 502 | `model_unavailable` | quota refunded |
| 503 | `ai_disabled`, `daily_cap` | |

Codes in `errors`: `net_mismatch`, `figure_not_in_document`, `missing_date`,
`settlement_before_trade`, `not_a_trade`, `multi_fill_detail`, `statement_total_mismatch`,
`no_positions`, `bad_model_output`, `extraction_failed`. `multi_fill_detail` is the breakdown of the
individual fills of a multi-fill order: import the summary note instead, or the trade would enter
twice. Codes in `warnings`: `invalid_isin`, `quantity_price_mismatch`, `position_value_mismatch`,
`no_text_layer`, `model_note`.

## Guests and quotas

    # connection string: the AZURE_TABLES_CONNECTION_STRING variable, or (macOS) a keychain item
    # named in scripts/guests_admin.py; for Azurite: export AZURE_TABLES_CONNECTION_STRING="UseDevelopmentStorage=true"
    python scripts/guests_admin.py add mario --quota 20   # prints the code, once
    python scripts/guests_admin.py list                   # this month's usage per guest
    python scripts/guests_admin.py quota mario 50         # new monthly quota, effective immediately
    python scripts/guests_admin.py reset mario            # zero this month's usage
    python scripts/guests_admin.py rotate mario           # new code, the old one stops working
    python scripts/guests_admin.py revoke mario           # (enable to reactivate)

The quota is monthly and resets by itself on the 1st (UTC). The code cannot be read back (only its
SHA-256 hash is stored): if a guest loses it, `rotate` or `revoke` and `add` under a new name. On
top of the per-guest quotas there is a global daily cap, `GLOBAL_DAILY_CAP`. The local dev server
does not use Table Storage: a single guest (`--code`, `--quota`), and counters reset on restart.

## Settings (SWA app settings)

| Name | Notes |
|---|---|
| LLM_MODEL | `provider:model`, default `anthropic:claude-haiku-5-5`; needs the matching `langchain-<provider>` package in `api/requirements.txt`. The model must support tool calling and PDF input |
| ANTHROPIC_API_KEY | key of the default provider (dedicated workspace with a spend cap); for another provider, its own variable |
| AZURE_TABLES_CONNECTION_STRING | Storage account for the Guests and Usage tables |
| AI_ENABLED | `false` = kill switch |
| GLOBAL_DAILY_CAP | total calls per day (default 200) |
| MAX_FAILED_PER_HOUR | wrong-code attempts per IP (default 20) |
| MODEL_TIMEOUT_S | default 18: two attempts fit in the ~45 s limit of managed functions |
| ALLOW_LOGIN / DEFAULT_USER_QUOTA | for public use: enables SWA login (off by default) |

The deploy workflow injects the Cloudflare Web Analytics beacon from the `CF_BEACON_TOKEN` GitHub
secret; without it the page ships without analytics.

## What is verified and what is not

Verified: extraction and validation on a real MPS notice, quotas and guests (including concurrency
on Azurite), the HTTP handler, the LangChain engine with a fake chat model, real model calls
(Haiku 5.5: correct on the first attempt, 2.5–5 s, no warnings) and a deploy on Azure Static Web
Apps with a managed Python function.

A battery of real documents can be run with `python scripts/eval_fixtures.py <folder>` (PDFs plus
an `expected.json`, kept outside git). On the author's private set of 43 MPS documents (purchases,
sales, a BTP with accrued interest, coupons, dividends, multi-fill orders, statements, contract
terms) plus 8 synthetic notices from fictional brokers, the result was 51/51 on the first attempt.
Manual check: `python scripts/try_extract.py notice.pdf [--model provider:model]` with the
provider key in the environment.

Not verified: real documents from brokers other than MPS (only synthetic ones), and providers other
than Anthropic.

## Privacy

The PDF contains names, address and account numbers and is sent to the model provider. It is not
stored or logged; the tables only hold code hashes and counters. The alpha disclaimer and the
privacy notice in the page must stay in place when opening to the public.
