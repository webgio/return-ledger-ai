from langchain_core.messages import AIMessage

from conftest import MPS_SALE, FakeChat, tool_msg
from core import extract_lc


def test_first_attempt_ok(mps_pdf_bytes):
    llm = FakeChat(tool_msg(MPS_SALE))
    out = extract_lc.extract(mps_pdf_bytes, llm)
    assert out.validation.ok and out.attempts == 1
    assert llm.bind_kwargs == {"tool_choice": "any"}
    assert [t["name"] for t in llm.tools] == ["record_trade", "record_statement"]
    assert all("parameters" in t for t in llm.tools)
    file_block = llm.calls[0][1].content[0]
    assert file_block["type"] == "file" and file_block["mime_type"] == "application/pdf"


def test_retry_feeds_errors_back(mps_pdf_bytes):
    wrong = {**MPS_SALE, "principal_amount": 3985.90, "stated_net_amount": 4002.99}
    llm = FakeChat(tool_msg(wrong), tool_msg(MPS_SALE))
    out = extract_lc.extract(mps_pdf_bytes, llm)
    assert out.validation.ok and out.attempts == 2
    assert "3985.90" in llm.calls[1][1].content[1]["text"]


def test_missing_tool_call_is_handled(mps_pdf_bytes):
    out = extract_lc.extract(mps_pdf_bytes, FakeChat(AIMessage(content="hi"), AIMessage(content="hi")))
    assert out.trade is None and [e.code for e in out.validation.errors] == ["bad_model_output"]


def test_gives_up_after_two_bad_attempts_but_reports_errors(mps_pdf_bytes):
    wrong = {**MPS_SALE, "stated_net_amount": 1.00}
    out = extract_lc.extract(mps_pdf_bytes, FakeChat(tool_msg(wrong), tool_msg(wrong)))
    assert not out.validation.ok and out.attempts == 2 and out.trade is not None


def test_schema_errors_are_handled(mps_pdf_bytes):
    invalid = tool_msg({"operation": "sell"})  # required amounts missing
    out = extract_lc.extract(mps_pdf_bytes, FakeChat(invalid, invalid))
    assert out.trade is None and [e.code for e in out.validation.errors] == ["bad_model_output"]
