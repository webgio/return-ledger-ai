"""Schema of what the model must extract from a broker trade notice.

Design rule: the model only *transcribes* the figures printed on the document
(principal, accrued interest, individual fee and tax lines, stated net amount).
Every derived number (gross, total fees, total tax, computed net) is calculated
by our code, so the model never does arithmetic we have to trust.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


def _blank_to_none(data: Any) -> Any:
    """Models sometimes send "" for a value that is not printed; treat it as missing."""
    if isinstance(data, dict):
        return {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in data.items()}
    return data


class LineItem(BaseModel):
    _blanks = model_validator(mode="before")(_blank_to_none)

    label: str = Field(description="Label exactly as printed, e.g. 'commissioni totali'")
    amount: Decimal = Field(ge=0, description="Absolute amount as a positive number")
    sign: Optional[Literal["+", "-"]] = Field(
        default=None,
        description="Sign printed next to the amount ('+' adds to the settled amount, "
        "'-' reduces it); null if no sign is printed",
    )


class ExtractedTrade(BaseModel):
    _blanks = model_validator(mode="before")(_blank_to_none)

    operation: Literal["buy", "sell", "coupon", "dividend", "other"] = Field(
        description="'buy'/'sell' for a purchase/sale notice, 'coupon'/'dividend' for an income "
        "payment notice; 'other' for anything else (position statement, contract terms, cost "
        "report, unreadable...)"
    )
    multi_fill_detail: bool = Field(
        default=False,
        description="True if the document only details the partial executions of an order and "
        "refers to a separate summary note of the same order",
    )
    instrument_name: Optional[str] = None
    isin: Optional[str] = None
    quantity: Optional[Decimal] = Field(
        default=None, description="Quantity actually traded (nominal value for bonds)"
    )
    unit_price: Optional[Decimal] = None
    trade_date: Optional[date] = Field(
        default=None, description="Execution date (YYYY-MM-DD)"
    )
    settlement_date: Optional[date] = Field(
        default=None,
        description="Value/settlement date ('valuta' / 'value date') when the cash moves (YYYY-MM-DD)",
    )
    currency: str = Field(default="EUR", description="Settlement currency (ISO 4217)")
    principal_amount: Decimal = Field(
        ge=0, description="Trade value before accrued interest, fees and taxes ('controvalore')"
    )
    accrued_interest: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Accrued interest amount ('controvalore rateo interessi'); 0 if none",
    )
    fee_items: list[LineItem] = Field(default_factory=list)
    tax_items: list[LineItem] = Field(
        default_factory=list,
        description="Every tax/withholding line (e.g. imposta sostitutiva), amounts as positive numbers",
    )
    stated_net_amount: Decimal = Field(
        ge=0, description="Final settled amount as printed ('controvalore finale')"
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Only figures or dates that are missing, illegible or ambiguous; usually empty",
    )


class Position(BaseModel):
    _blanks = model_validator(mode="before")(_blank_to_none)

    name: str = Field(description="Security name as printed")
    code: Optional[str] = Field(default=None, description="ISIN or the bank's security code as printed")
    quantity: Optional[Decimal] = Field(default=None, description="Quantity held (nominal value for bonds)")
    market_price: Optional[Decimal] = Field(
        default=None, description="Unit market price ('valore di mercato'); bonds per 100 nominal"
    )
    accrued_rate: Decimal = Field(
        default=Decimal("0"), ge=0,
        description="Accrued interest as printed ('rateo'), per 100 nominal; 0 if none",
    )
    value: Decimal = Field(
        ge=0, description="Position value in the settlement currency ('controvalore euro')"
    )


class ExtractedStatement(BaseModel):
    """A periodic position statement: becomes a `valuation` row, for the whole account or
    for one position."""

    _blanks = model_validator(mode="before")(_blank_to_none)

    statement_date: Optional[date] = Field(
        default=None, description="Date the positions are valued at ('Estratto al', YYYY-MM-DD)"
    )
    currency: str = Field(default="EUR", description="Currency of the values (ISO 4217)")
    positions: list[Position] = Field(default_factory=list)
    stated_total: Decimal = Field(
        ge=0, description="Total value as printed ('saldo totale strumenti finanziari')"
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Only figures or dates that are missing, illegible or ambiguous; usually empty",
    )


def tool_schema(model: type[BaseModel] = ExtractedTrade) -> dict[str, Any]:
    """JSON schema for a tool call.

    Pydantic renders Decimal as anyOf[number, string-with-pattern]; we collapse it to a
    plain number so the model returns clean JSON numbers.
    """
    schema = model.model_json_schema()

    def simplify(node: Any) -> Any:
        if isinstance(node, dict):
            any_of = node.get("anyOf")
            if isinstance(any_of, list):
                kinds = {n.get("type") for n in any_of if isinstance(n, dict)}
                if "number" in kinds and "string" in kinds:
                    rest = {k: v for k, v in node.items() if k != "anyOf"}
                    rest["type"] = "number"
                    return simplify(rest)
            return {k: simplify(v) for k, v in node.items()}
        if isinstance(node, list):
            return [simplify(v) for v in node]
        return node

    return simplify(schema)
