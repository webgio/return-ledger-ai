"""HTTP entry point (Azure Functions, Python v2 model).

POST /api/extract   body: raw PDF bytes   header: x-guest-code: <code>
Thin on purpose: all real logic lives in `core/`, which knows nothing about Azure Functions.
"""
from __future__ import annotations

import io
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import azure.functions as func

from core import gate
from core.extract_lc import extract, make_chat_model
from core.ledger import to_row, to_valuations
from core.store import MemoryStore, TableStore
from core.validate import Issue, compute

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

MAX_BYTES = int(os.environ.get("MAX_PDF_BYTES", str(5 * 1024 * 1024)))
MAX_PAGES = int(os.environ.get("MAX_PDF_PAGES", "6"))

_store: Any = None
_llm: Any = None


def get_store() -> Any:
    global _store
    if _store is None:
        conn = os.environ.get("AZURE_TABLES_CONNECTION_STRING")
        _store = TableStore(conn) if conn else MemoryStore()
    return _store


def get_llm() -> Any:
    global _llm
    if _llm is None:
        _llm = make_chat_model()
    return _llm


def error(status: int, code: str, message: str, **extra: Any) -> func.HttpResponse:
    return reply(status, {"error": {"code": code, "message": message}, **extra})


def issues(items: list[Issue]) -> list[dict[str, str]]:
    return [asdict(i) for i in items]


def reply(status: int, payload: dict[str, Any]) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False, default=str),
        status_code=status,
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


def check_pdf(body: bytes) -> None:
    if len(body) > MAX_BYTES:
        raise gate.GateError(413, "file_too_large",
                             f"File too large (maximum {MAX_BYTES // 1024 // 1024} MB).")
    if not body.startswith(b"%PDF-"):
        raise gate.GateError(415, "not_pdf", "The file is not a PDF.")
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            raise gate.GateError(422, "pdf_encrypted", "The PDF is password protected.")
        pages = len(reader.pages)
    except gate.GateError:
        raise
    except Exception:
        raise gate.GateError(422, "pdf_unreadable", "The PDF cannot be read.")
    if pages > MAX_PAGES:
        raise gate.GateError(413, "too_many_pages", f"Too many pages (maximum {MAX_PAGES}).")


@app.route(route="extract", methods=["POST"])
def extract_trade(req: func.HttpRequest) -> func.HttpResponse:
    if os.environ.get("AI_ENABLED", "true").lower() != "true":
        return error(503, "ai_disabled", "The AI import is temporarily disabled.")

    now = datetime.now(timezone.utc)
    store = get_store()
    subject = None
    try:
        subject = gate.authenticate(req.headers, store, now)
        body = req.get_body()
        check_pdf(body)  # invalid uploads must not consume quota
        remaining = gate.reserve(subject, store, now)
    except gate.GateError as e:
        return error(e.status, e.code, e.message)

    try:
        outcome = extract(body, get_llm())
    except Exception:
        # Never log the document or the model output; only who and what kind of failure.
        logging.exception("model call failed for %s", subject.label)
        gate.release(subject, store, now)
        return error(502, "model_unavailable", "The extraction service is not responding. Try again.")

    logging.info("extract subject=%s ok=%s attempts=%s", subject.label,
                 outcome.validation.ok, outcome.attempts)

    if outcome.statement is not None:
        return reply(200, {
            "kind": "statement",
            "status": "ok" if outcome.validation.ok else "needs_review",
            "valuation": to_valuations(outcome.statement),
            "errors": issues(outcome.validation.errors),
            "warnings": issues(outcome.validation.warnings),
            "remaining": remaining,
        })
    if outcome.trade is None:
        return error(422, "unreadable_document", "Could not read the document.",
                     errors=issues(outcome.validation.errors), remaining=remaining)
    if outcome.trade.operation == "other" or outcome.trade.multi_fill_detail:
        first = outcome.validation.errors[0]
        return error(422, first.code, first.message, remaining=remaining)

    c = compute(outcome.trade)
    return reply(200, {
        "kind": "trade",
        "status": "ok" if outcome.validation.ok else "needs_review",
        "row": to_row(outcome.trade),
        "details": {
            "instrument": outcome.trade.instrument_name,
            "isin": outcome.trade.isin,
            "trade_date": outcome.trade.trade_date,
            "settlement_date": outcome.trade.settlement_date,
            "gross": str(c.gross), "fees": str(c.fees), "taxes": str(c.taxes),
            "net": str(c.net),
        },
        "errors": issues(outcome.validation.errors),
        "warnings": issues(outcome.validation.warnings),
        "remaining": remaining,
    })
