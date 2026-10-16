import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.models import ExtractedTrade

DEFAULT_PDF = "/tmp/claude-0/-home-claude/0453ccfa-05be-5674-823f-7636680ae0a1/scratchpad/pdf-in/mps.pdf"
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


def tool_response(data: dict, name: str = "record_trade"):
    block = SimpleNamespace(type="tool_use", name=name, input=data)
    return SimpleNamespace(content=[block])


class FakeClient:
    """Mimics anthropic.Anthropic().messages.create, returning queued responses."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)
