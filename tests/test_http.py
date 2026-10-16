"""End-to-end through the real handler (function_app.py) with a fake model client."""
import json
import os
from datetime import datetime, timezone

import azure.functions as func
import pytest

import function_app
from conftest import MPS_SALE, FakeClient, tool_response
from core import gate
from core.store import MemoryStore


@pytest.fixture
def setup(monkeypatch):
    store = MemoryStore()
    store.put_guest(gate.hash_code("code-1"), "mario", 3)
    monkeypatch.setattr(function_app, "_store", store)
    monkeypatch.delenv("AI_ENABLED", raising=False)
    return store


def call(body, code="code-1", client=None, monkeypatch=None):
    if client is not None:
        function_app._client = client
    headers = {"x-forwarded-for": "5.6.7.8"}
    if code:
        headers["x-guest-code"] = code
    req = func.HttpRequest("POST", "/api/extract", headers=headers, body=body)
    resp = function_app.extract_trade.build().get_user_function()(req)
    return resp.status_code, json.loads(resp.get_body())


def test_happy_path_returns_row(setup, mps_pdf_bytes):
    status, data = call(mps_pdf_bytes, client=FakeClient(tool_response(MPS_SALE)))
    assert status == 200 and data["status"] == "ok"
    assert data["row"] == {"date": "2025-08-07", "type": "withdraw", "amount": 4010.14,
                           "fees": 2.99, "tax": 4.26,
                           "note": "Sale BTP GREEN-APR45 (IT0005438004) qty 6000"}
    assert data["remaining"] == 2 and data["errors"] == []


def test_inconsistent_extraction_is_flagged_for_review(setup, mps_pdf_bytes):
    wrong = {**MPS_SALE, "stated_net_amount": 1.0}
    status, data = call(mps_pdf_bytes, client=FakeClient(tool_response(wrong), tool_response(wrong)))
    assert status == 200 and data["status"] == "needs_review"
    assert [e["code"] for e in data["errors"]] == ["net_mismatch"]
    assert "do not add up" in data["errors"][0]["message"]


def test_model_failure_gives_credit_back(setup, mps_pdf_bytes):
    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("upstream down")
    status, data = call(mps_pdf_bytes, client=Boom)
    assert status == 502 and data["error"]["code"] == "model_unavailable"
    assert setup.get_count("guest:mario", datetime.now(timezone.utc).strftime("%Y-%m")) == 0


def test_rejections_do_not_consume_quota(setup):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert call(b"not a pdf at all") == (415, {"error": {"code": "not_pdf",
                                                          "message": "The file is not a PDF."}})
    assert call(b"%PDF-1.4" + b"x" * (6 * 1024 * 1024))[1]["error"]["code"] == "file_too_large"
    assert call(b"%PDF-1.4 broken")[1]["error"]["code"] == "pdf_unreadable"
    assert setup.get_count("guest:mario", month) == 0


def test_auth_failures(setup):
    assert call(b"%PDF-", code=None) == (401, {"error": {"code": "code_required",
                                                         "message": "An access code is required."}})
    assert call(b"%PDF-", code="wrong")[1]["error"]["code"] == "invalid_code"


def test_kill_switch(setup, monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "false")
    assert call(b"%PDF-")[1]["error"]["code"] == "ai_disabled"


def test_quota_runs_out(setup, mps_pdf_bytes):
    for _ in range(3):
        status, _ = call(mps_pdf_bytes, client=FakeClient(tool_response(MPS_SALE)))
        assert status == 200
    status, data = call(mps_pdf_bytes, client=FakeClient(tool_response(MPS_SALE)))
    assert status == 429 and data["error"]["code"] == "quota_exhausted"


def test_multi_fill_detail_is_refused_with_its_code(setup, mps_pdf_bytes):
    detail = {**MPS_SALE, "multi_fill_detail": True}
    status, data = call(mps_pdf_bytes, client=FakeClient(tool_response(detail)))
    assert status == 422 and data["error"]["code"] == "multi_fill_detail"


def test_statement_returns_valuation_options(setup, mps_pdf_bytes):
    statement = {"statement_date": "2025-12-31", "stated_total": 4002.89,
                 "positions": [{"name": "BTP GREEN-APR45", "value": 4002.89}]}
    status, data = call(mps_pdf_bytes, client=FakeClient(tool_response(statement, "record_statement")))
    # figures exist in the MPS sale PDF text, so the checks pass
    assert status == 200 and data["kind"] == "statement" and data["status"] == "ok"
    assert data["valuation"]["date"] == "2025-12-31"
    assert [o["value"] for o in data["valuation"]["options"]] == [4002.89, 4002.89]
