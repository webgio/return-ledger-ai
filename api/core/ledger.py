"""Map a validated extraction to a Return Ledger row.

Conventions (same as the app): `amount` is the GROSS flow actually moved on the instrument
(principal + accrued interest); fees and taxes go in their own columns; the row date is the
settlement ('valuta') date because that is when the cash moves. A purchase is a `deposit`
into the ledger; a sale, coupon or dividend is a `withdraw` (cash leaving the portfolio). A
tax credit (e.g. on accrued interest paid when buying a bond) is a negative `tax`.
"""
from __future__ import annotations

from typing import Any

from .models import ExtractedStatement, ExtractedTrade
from .validate import compute, is_buy, q2


def to_row(t: ExtractedTrade) -> dict[str, Any]:
    c = compute(t)
    d = t.settlement_date or t.trade_date
    verb = {"buy": "Purchase", "sell": "Sale", "coupon": "Coupon", "dividend": "Dividend"}[t.operation]
    parts = [verb]
    if t.instrument_name:
        parts.append(t.instrument_name)
    if t.isin:
        parts.append(f"({t.isin})")
    if t.quantity is not None:
        parts.append(f"qty {t.quantity.normalize():f}")
    return {
        "date": d.isoformat() if d else None,
        "type": "deposit" if is_buy(t) else "withdraw",
        "amount": float(c.gross),
        "fees": float(c.fees),
        "tax": float(c.taxes),
        "note": " ".join(parts),
    }


def to_valuations(s: ExtractedStatement) -> dict[str, Any]:
    """A statement offers one `valuation` row per choice: the whole account (for a ledger that
    tracks the full deposit) or a single position (for a per-instrument ledger)."""
    options = [{"label": "Whole account", "value": float(q2(s.stated_total)),
                "note": "Statement: whole account"}]
    for p in s.positions:
        label = f"{p.name} ({p.code})" if p.code else p.name
        options.append({"label": label, "value": float(q2(p.value)), "note": f"Statement: {p.name}"})
    return {"date": s.statement_date.isoformat() if s.statement_date else None, "options": options}
