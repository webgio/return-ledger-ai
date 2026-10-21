"""Deterministic checks on an extraction. No model involved.

Three layers:
1. arithmetic: gross -/+ fees/taxes must equal the net amount printed on the document
2. sanity: dates, ISIN shape, quantity x price vs principal
3. grounding: every figure the model returned must literally appear in the PDF text
   (catches hallucinated numbers; skipped for scanned PDFs with no text layer)

Position statements get the same three layers (`check_statement`): positions add up to the
printed total, quantity x price (+ accrued) matches each position value, figures are in the text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .models import ExtractedStatement, ExtractedTrade, LineItem

CENT = Decimal("0.01")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
NATIONAL_CODE_RE = re.compile(r"^[A-Z0-9]{6,10}$")  # CUSIP (9), SEDOL (7), ...
NUMBER_TOKEN_RE = re.compile(r"\d[\d.,]*\d|\d")
# 21 372,00 / 21'372.00: thousands grouped by a (non-breaking) space or an apostrophe
GROUPED_NUMBER_RE = re.compile(r"\d{1,3}(?:[ \u00a0\u202f'\u2019]\d{3})+(?:[.,]\d+)?")


MAX_NOTES = 3
MAX_NOTE_CHARS = 120


def model_notes(warnings: list[str]) -> list[Issue]:
    """Free text from the model is the one place personal data from the PDF could leak back
    out, so cap how much of it reaches the client."""
    notes = [" ".join(w.split()) for w in warnings]
    return [
        Issue("model_note", n if len(n) <= MAX_NOTE_CHARS else n[: MAX_NOTE_CHARS - 1] + "…")
        for n in notes[:MAX_NOTES] if n
    ]


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Computed:
    gross: Decimal
    fees: Decimal
    taxes: Decimal
    net: Decimal


def is_buy(t: ExtractedTrade) -> bool:
    """Cash goes into the position on a buy; on a sale, coupon or dividend it comes out."""
    return t.operation == "buy"


def cost(t: ExtractedTrade, item: LineItem) -> Decimal:
    """An item's cost to the investor: positive for a charge, negative for a credit.

    Printed signs are relative to the settled amount: '+' makes it larger, '-' smaller. On a
    buy a larger settlement is a charge; on a sale or income a smaller one is. Without a
    printed sign the item is assumed to be a charge (the usual case).
    """
    if item.sign is None:
        return item.amount
    grows_settlement = item.sign == "+"
    return item.amount if grows_settlement == is_buy(t) else -item.amount


def compute(t: ExtractedTrade) -> Computed:
    gross = t.principal_amount + t.accrued_interest
    fees = sum((cost(t, i) for i in t.fee_items), Decimal("0"))
    taxes = sum((cost(t, i) for i in t.tax_items), Decimal("0"))
    # buy: cash paid = gross plus costs. sale/income: cash received = gross minus costs.
    net = gross + fees + taxes if is_buy(t) else gross - fees - taxes
    return Computed(q2(gross), q2(fees), q2(taxes), q2(net))


@dataclass(frozen=True)
class Issue:
    """`code` is stable (clients translate from it); `message` is the English default text."""

    code: str
    message: str


@dataclass
class Validation:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def numbers_in_text(text: str) -> set[Decimal]:
    """Every number in the text, read under both Italian (1.234,56) and English (1,234.56)
    conventions, rounded to cents, plus space/apostrophe-grouped thousands (21 372,00). A
    superset on purpose: used only for membership tests."""
    found: set[Decimal] = set()
    tokens = NUMBER_TOKEN_RE.findall(text)
    tokens += [re.sub(r"[ \u00a0\u202f'\u2019]", "", m) for m in GROUPED_NUMBER_RE.findall(text)]
    for tok in tokens:
        candidates = (
            tok.replace(".", "").replace(",", "."),  # Italian
            tok.replace(",", ""),  # English
        )
        for c in candidates:
            try:
                found.add(q2(Decimal(c)))
            except Exception:
                continue
    return found


def check(t: ExtractedTrade, pdf_text: str | None = None) -> Validation:
    v = Validation()

    if t.multi_fill_detail:
        v.errors.append(Issue(
            "multi_fill_detail",
            "This only details the partial executions of an order: import the order's summary "
            "note instead, or the trade would be counted twice.",
        ))
        return v
    if t.operation == "other":
        v.errors.append(Issue(
            "not_a_trade",
            "The document is not a buy/sell confirmation or a coupon/dividend notice.",
        ))
        return v

    c = compute(t)

    # 1. arithmetic
    if abs(c.net - q2(t.stated_net_amount)) > CENT:
        v.errors.append(Issue(
            "net_mismatch",
            f"Amounts do not add up: gross {c.gross} "
            f"{'+' if is_buy(t) else '-'} fees {c.fees} and taxes {c.taxes} "
            f"= {c.net}, but the document states {q2(t.stated_net_amount)}.",
        ))

    # 2. sanity
    if not (t.settlement_date or t.trade_date):
        v.errors.append(Issue("missing_date", "No date found in the document."))
    if (t.operation in ("buy", "sell") and t.trade_date and t.settlement_date
            and t.settlement_date < t.trade_date):
        v.errors.append(Issue("settlement_before_trade",
                              "The settlement date is before the trade date."))
    if t.isin and not ISIN_RE.match(t.isin) and not (len(t.isin) != 12 and NATIONAL_CODE_RE.match(t.isin)):
        v.warnings.append(Issue("invalid_isin", f"ISIN '{t.isin}' is not well formed."))
    if t.quantity is not None and t.unit_price is not None:
        by_unit = t.quantity * t.unit_price
        by_hundred = by_unit / Decimal(100)  # bonds are quoted per 100 nominal
        tol = max(Decimal("0.05"), t.principal_amount * Decimal("0.001"))
        if min(abs(by_unit - t.principal_amount), abs(by_hundred - t.principal_amount)) > tol:
            v.warnings.append(Issue(
                "quantity_price_mismatch",
                "Quantity × price does not match the trade value: please check.",
            ))
    v.warnings.extend(model_notes(t.warnings))

    # 3. grounding
    figures = {"principal": t.principal_amount, "net amount": t.stated_net_amount}
    if t.accrued_interest > 0:
        figures["accrued interest"] = t.accrued_interest
    for item in [*t.fee_items, *t.tax_items]:
        figures[item.label] = item.amount
    _grounding(v, figures, pdf_text)
    return v


def _grounding(v: Validation, figures: dict[str, Decimal], pdf_text: str | None) -> None:
    if pdf_text and len(pdf_text.strip()) >= 200:
        present = numbers_in_text(pdf_text)
        for label, amount in figures.items():
            if q2(amount) not in present:
                v.errors.append(Issue(
                    "figure_not_in_document",
                    f"The figure {q2(amount)} ('{label}') does not appear in the document text.",
                ))
    else:
        v.warnings.append(Issue(
            "no_text_layer",
            "The document has no selectable text (a scan?): check the figures by hand.",
        ))


def check_statement(s: ExtractedStatement, pdf_text: str | None = None) -> Validation:
    v = Validation()

    if not s.statement_date:
        v.errors.append(Issue("missing_date", "No date found in the document."))
    if not s.positions:
        v.errors.append(Issue("no_positions", "No positions found in the statement."))

    # 1. arithmetic: positions add up to the printed total (each value is already rounded,
    # so allow a cent per position at most)
    total = q2(sum((p.value for p in s.positions), Decimal("0")))
    tol = max(CENT, CENT * len(s.positions))
    if s.positions and abs(total - q2(s.stated_total)) > tol:
        v.errors.append(Issue(
            "statement_total_mismatch",
            f"Positions add up to {total}, but the statement total is {q2(s.stated_total)}.",
        ))

    # 2. sanity: quantity x price (+ accrued interest for bonds) matches each position value
    for p in s.positions:
        if p.quantity is None or p.market_price is None:
            continue
        by_unit = p.quantity * p.market_price
        by_hundred = p.quantity * (p.market_price + p.accrued_rate) / Decimal(100)
        ptol = max(Decimal("0.05"), p.value * Decimal("0.001"))
        if min(abs(by_unit - p.value), abs(by_hundred - p.value)) > ptol:
            v.warnings.append(Issue(
                "position_value_mismatch",
                f"{p.name}: quantity x price does not match the position value {q2(p.value)}.",
            ))
    v.warnings.extend(model_notes(s.warnings))

    # 3. grounding
    figures = {"statement total": s.stated_total}
    for p in s.positions:
        figures[p.name] = p.value
    _grounding(v, figures, pdf_text)
    return v
