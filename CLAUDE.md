# Return Ledger — project context

Read this first. It summarizes decisions made in an earlier design conversation so work can continue
from here without re-explaining. The owner is a .NET/Azure/data-engineering developer, comfortable in
Italian and English; Azure is the chosen cloud (AWS was considered and set aside).

## What this is
- **Return Ledger**: free, client-side portfolio performance tracker (TWR + XIRR, gross and net of
  fees/tax). Single self-contained page, no backend, data in browser localStorage. Currently
  `app/index.html` (exported from a Claude Artifact; ~1500 lines, vanilla JS).
  Differentiators to protect: zero install, one screen, a couple of honest indicators, no accounts.
- **New feature (this repo's `api/`)**: upload a broker/bank trade notice PDF -> AI proposes one
  ledger row -> user confirms. First test document: MPS "Nota informativa" for a BTP sale.

## Domain rules (do not break)
- One tool; no gross/net or per-asset-type variants.
- `amount` is always the GROSS flow actually moved (for bonds: controvalore + accrued interest);
  `fees` and `tax` are separate columns; net = gross -/+ costs. TWR stays a pure market measure.
- Row date = settlement/value date ("valuta"), because that is when cash moves.
- Purchase = `deposit` into the ledger; sale, coupon and dividend = `withdraw` (cash leaving the
  portfolio; decided 2026-10-12, the goal is the portfolio's own return). Valuation rows are separate.
- `fees`/`tax` carry the sign of the cost to the investor: a tax credit (e.g. substitute tax on
  accrued interest paid when buying a bond) is a negative `tax`. The model transcribes the sign
  printed next to each line; code derives the cost sign (`core/validate.py: cost`).
- Multiple ledgers exist (store key `return-ledger-v2`: `{ledgers:[{id,name,rows}], activeId}`).
  A new row is added via `addRow(prefill)` in `app/index.html`.
- XIRR was cross-checked against Excel's XIRR on real data (gross 8.774%, net 8.653%).

## Architecture decisions for the AI feature
- Azure Static Web App + managed Python function (`api/`). Same origin under `/api`, no CORS.
- `api/core/*` has no Azure dependency (pure functions); `function_app.py` is a thin handler. Keep it so:
  the cloud stays swappable.
- The model only TRANSCRIBES printed figures (principal, accrued interest, each fee/tax line, stated
  net). Code does all arithmetic and checks: net must match, every figure must appear in the PDF
  text, then one retry with the errors fed back. Result is `ok` or `needs_review`; the UI must show
  errors/warnings prominently and never auto-save.
- Access control for now: per-guest access codes stored as SHA-256 hashes in Table Storage
  (`scripts/guests_admin.py`), monthly quota per guest, global daily cap, failed-attempt throttle,
  `AI_ENABLED` kill switch. Public login (SWA auth) is prepared but off (`ALLOW_LOGIN`).
- API key lives only server-side in the SWA app settings (managed functions support neither Key Vault
  references nor managed identity, checked 2026-10-14), dedicated provider workspace with spend cap.
- Privacy: PDFs contain names/addresses/account numbers and go to the model provider. Never store or
  log documents or model output. Disclose this before opening to the public. The tool schemas have
  no field for personal data; the only free text coming back is `warnings` (`model_note`), which
  `core/validate.py: model_notes` caps at 3 notes of 120 characters (2026-10-16).

## State
- Done and tested: extraction/validation, ledger mapping, gate/quotas (incl. Azurite concurrency test),
  HTTP handler and extraction with a fake LangChain chat model. `pytest` from the repo root.
- Real model call verified on the MPS PDF (2026-10-10): Haiku 5.5 (default) and Haiku 4.5 both
  exact on the first attempt, no warnings, 2.5-4.8 s. Manual runner: `scripts/try_extract.py`.
  The local key lives in the macOS keychain; spend cap is set on
  the dedicated Anthropic workspace (limits are per workspace, not per key).
- Fixture eval (2026-10-14, Haiku 5.5): 42/42 MPS documents correct on the first attempt
  (buys, sales, BTP buy with tax credit, coupons, dividends, multi-fill summary and detail,
  7 statements as valuations, contract terms, cost report), slowest ~5 s. Prompt lessons: the
  model added false arithmetic warnings on statements and once took the multi-fill SUMMARY for
  the detail; both fixed with explicit prompt rules, keep re-running the battery after prompt edits. Fix found on the way: the model
  sometimes sends "" for a missing date -> blank strings are treated as null in `models.py`.
- Checked in the docs (2026-10-14): managed functions support `python:3.11` (also 3.9/3.10),
  HTTP triggers only, 45 s max per API request, available on the Free plan.
- Deployed (2026-10-16): SWA (Free, West Europe) with a custom apex domain; the default
  `*.azurestaticapps.net` host is random by design and stays active. DNS provider without ALIAS
  records: the apex uses a TXT validation record on `@` plus an A record on `@` -> the SWA
  `stableInboundIP` (single regional host, no global distribution; fine for this app). Never a CNAME
  on `@`: it shadows the TXT and breaks resolution. Certificate managed by SWA (DigiCert),
  auto-renewed. No `www` yet. GitHub Actions deploys on push to `main`. Python v2 model runs fine as
  a managed function (Oryx, 3.11). Real import on the live site: correct row, 7.4 s incl. cold start.
  App settings on the SWA: `ANTHROPIC_API_KEY` (separate prod key, same workspace),
  `AZURE_TABLES_CONNECTION_STRING`. Locally, secrets live in the macOS keychain (dev key, prod
  tables connection string, the owner's guest code).
  Keychain tip: `security ... -w` prompts cap input at 128 chars; use `-w "$(pbpaste)"` instead.
- The Azure CLI may only see the wrong tenant/subscription by default: `az login --tenant ...`
  before any `az` command against the production subscription.
- Number format (2026-10-16): the prompt now asks the model to detect Italian vs English
  separators from the whole document. A synthetic US-style notice (USD, 1,234.56, MM/DD dates)
  was exact on the first attempt (Haiku 5.5); still no real non-MPS documents.
- International prep (2026-10-21): prompt generalized (any language, Italian + English terms side by
  side, fee vs tax definitions incl. stamp duty/FTT/withholding, pay date for income, ignore ex/record
  dates); alpha disclaimer in the page badges and AI panel. Fixture battery now 51/51 on the first
  attempt (Haiku 5.5): the 43 above plus 8 synthetic notices from fictional brokers (DE buy, FR buy
  with FTT, ES dividend with retention, UK sale in GBP, US dividend with withholding, EN statement,
  EN bond buy with accrued interest, DE non-trade). They live in the fixtures folder (outside git).
  Found on the way: the grounding check missed space-grouped thousands ("21 372,00"); fixed in
  `core/validate.py: numbers_in_text` (space, NBSP, apostrophe). Currency is deliberately not
  handled (2026-10-21): the app shows plain numbers without a symbol, never converts, and the
  footer asks users to keep one currency per ledger; the `non_eur_currency` warning was removed.
- LangChain only (2026-10-29): the direct Anthropic SDK path is gone. `api/core/extract_lc.py` is
  the single engine, provider-agnostic: no vendor import, the model is a `provider:model` string
  (`LLM_MODEL`, default `anthropic:claude-haiku-5-5`, or `--model` on the scripts) resolved by
  `init_chat_model`; neutral tool schemas, `bind_tools(..., tool_choice="any")`, PDF as a standard
  file block. `core/extract.py` keeps the prompt, tool definitions and the `run_extraction` retry
  loop. `function_app.py` uses it (`get_llm`). Dependencies: `langchain` + `langchain-anthropic` in
  `api/requirements.txt` (`anthropic` is no longer a direct dependency; `ANTHROPIC_API_KEY` is still
  the provider key). Previous eval with this engine: 51/51 (Haiku 5.5, slowest 4.3 s); real call on
  the anonymized MPS PDF re-checked 2026-10-29. NOT verified: the deploy of this build on SWA (bigger
  package, cold start vs the 45 s limit); other providers (no keys; they need tools + PDF input).
  Note: the `ANTHROPIC_MODEL` app setting is replaced by `LLM_MODEL`; update it on the SWA before deploying.
- Analytics (2026-10-29): the Cloudflare Web Analytics token is not in the repo. `app/index.html` has an
  `<!-- ANALYTICS: ... -->` marker; the deploy workflow injects the beacon after it when the GitHub
  secret `CF_BEACON_TOKEN` is set (no secret = no analytics, e.g. in forks).
- Test fixture (2026-10-29): `tests/fixtures/mps_btp_sale_anonymized.pdf` is the MPS BTP sale notice
  with names, address, deposit/account/order numbers and metadata replaced (PyMuPDF redaction); the
  only PDF allowed in git. Originals stay outside the repo.
- NOT verified: real documents from brokers other than MPS (only the synthetic ones above).
- Frontend integration done (2026-10-10) in `app/index.html` (section "AI import" at the end of
  the script): verified end to end in headless Chrome against `scripts/dev_server.py` with the
  real model (wrong code, real MPS import -> addRow, mocked needs_review gating, light/dark,
  390 px width, page without API). The page now has doctype/charset/viewport for standalone
  serving; the footer no longer claims "nothing is uploaded" unconditionally.

## Next steps (suggested order)
1. ~~Run with a real key against the MPS PDF~~ (done, see State).
2. ~~Frontend in `app/index.html`~~ (done, see State). Keep the page self-contained and usable
   without the AI feature.
3. Real notices as fixtures: `scripts/eval_fixtures.py <fixtures folder>`
   (43 MPS documents + expected.json, outside git). Still wanted: other brokers.
3b. ~~Position statements -> `valuation` row~~ (done 2026-10-14): second tool `record_statement`
   (model picks the tool, `tool_choice: any`), `check_statement`, `to_valuations`; the UI lets the
   user pick the whole account or one position. Multi-fill detail notes (Mod. 152) are refused
   (`multi_fill_detail`): the summary note is imported. Not handled: a trade settling after the
   statement date is not in the statement total (the bank says so); the user must mind it.
   Note for model upgrades: Sonnet 5.5 / Opus 5.5 reject forced `tool_choice` (`any`/`tool`);
   switching to them means `auto` + `strict: true` tools.
4. ~~Deploy to SWA~~ and custom domain (done 2026-10-16). Still open: budget alert on the
   resource group, optional `www` subdomain, optional Application Insights.
5. Optional learning exercise: rewrite the extract -> validate -> retry loop in `core/extract.py` as a
   LangGraph graph and compare complexity (the plain loop in `run_extraction` is the baseline and works).

## Conventions
- Code and comments in English. User-facing strings in English for the first version (demos with
  English-speaking users); an Italian version comes later. Every API error/warning carries a stable
  `code` plus an English `message`, so the frontend can translate from the code without backend
  changes (`model_note` warnings are free text from the model). The README is in English (it was in Italian
  for the owner). Money with `Decimal`, never float, except at the JSON boundary. Don't add
  dependencies without need.
- Workload identity federation was considered instead of an API key and deferred: SWA managed
  functions (probably) lack managed identity; revisit if moving to a linked Function App or going
  public.
- Tests that need the real PDF read it from `MPS_PDF` and skip otherwise; never commit real PDFs (only the anonymized one in tests/fixtures).
