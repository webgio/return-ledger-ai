from conftest import MPS_SALE, FakeClient, tool_response
from core.extract import TOOL_NAME, extract


def test_first_attempt_ok(mps_pdf_bytes):
    client = FakeClient(tool_response(MPS_SALE))
    out = extract(mps_pdf_bytes, client, model="m")
    assert out.validation.ok and out.attempts == 1 and out.pdf_text_available
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "any"}
    assert [t["name"] for t in call["tools"]] == [TOOL_NAME, "record_statement"]
    doc = call["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"


def test_retry_corrects_a_wrong_figure(mps_pdf_bytes):
    wrong = {**MPS_SALE, "principal_amount": 3985.90, "stated_net_amount": 4002.99}
    client = FakeClient(tool_response(wrong), tool_response(MPS_SALE))
    out = extract(mps_pdf_bytes, client, model="m")
    assert out.validation.ok and out.attempts == 2
    retry_text = client.calls[1]["messages"][0]["content"][1]["text"]
    assert "failed these automatic checks" in retry_text and "3985.90" in retry_text


def test_gives_up_after_two_bad_attempts_but_reports_errors(mps_pdf_bytes):
    wrong = {**MPS_SALE, "stated_net_amount": 1.00}
    out = extract(mps_pdf_bytes, FakeClient(tool_response(wrong), tool_response(wrong)), model="m")
    assert not out.validation.ok and out.attempts == 2 and out.trade is not None


def test_missing_tool_call_and_schema_errors_are_handled(mps_pdf_bytes):
    from types import SimpleNamespace
    empty = SimpleNamespace(content=[])
    invalid = tool_response({"operation": "sell"})  # required amounts missing
    out = extract(mps_pdf_bytes, FakeClient(empty, invalid), model="m")
    assert out.trade is None and [e.code for e in out.validation.errors] == ["bad_model_output"]
