import os
from decimal import Decimal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from core.models import ExtractedTrade

# Anonymized copy of an MPS BTP-sale notice (personal data replaced); MPS_PDF overrides it.
DEFAULT_PDF = Path(__file__).parent / "fixtures" / "mps_btp_sale_anonymized.pdf"
PDF_PATH = Path(os.environ.get("MPS_PDF", DEFAULT_PDF))

# What a correct extraction of the MPS BTP-sale notice looks like (figures as printed).
MPS_SALE = {
    "operation": "sell",
    "instrument_name": "BTP GREEN-APR45",
    "isin": "IT0005438004",
    "quantity": 6000,
    "unit_price": 66.43,
    "trade_date": "2025-08-05",
    "settlement_date": "2025-08-07",
    "currency": "EUR",
    "principal_amount": 3985.80,
    "accrued_interest": 24.34,
    "fee_items": [{"label": "commissioni totali", "amount": 2.99}],
    "tax_items": [
        {"label": "imposta sostit. su rateo interessi", "amount": 3.04},
        {"label": "imposta sostit. su scarto di emissione", "amount": 1.22},
    ],
    "stated_net_amount": 4002.89,
    "warnings": [],
}


@pytest.fixture
def mps_sale():
    return ExtractedTrade.model_validate(MPS_SALE)


@pytest.fixture(scope="session")
def mps_pdf_bytes():
    if not PDF_PATH.exists():
        pytest.skip("PDF di prova non disponibile (imposta MPS_PDF)")
    return PDF_PATH.read_bytes()


class FakeChat:
    """Mimics a LangChain chat model after bind_tools, returning queued AI messages."""

    def __init__(self, *replies):
        self.queue = list(replies)
        self.calls = []
        self.bind_kwargs = None

    def bind_tools(self, tools, **kwargs):
        self.tools, self.bind_kwargs = tools, kwargs
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return self.queue.pop(0)


def tool_msg(data: dict, name: str = "record_trade"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": data, "id": "t1"}])
