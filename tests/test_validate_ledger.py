from decimal import Decimal

from core.ledger import to_row
from core.models import ExtractedTrade, tool_schema
from core.validate import check, compute, numbers_in_text
from conftest import MPS_SALE


def test_compute_matches_broker_note(mps_sale):
    c = compute(mps_sale)
    assert (c.gross, c.fees, c.taxes, c.net) == (
        Decimal("4010.14"), Decimal("2.99"), Decimal("4.26"), Decimal("4002.89"))


def test_row_matches_what_was_entered_by_hand(mps_sale):
    # Same row that already exists in the ledger for this sale.
    assert to_row(mps_sale) == {
        "date": "2025-08-07", "type": "withdraw", "amount": 4010.14,
        "fees": 2.99, "tax": 4.26,
        "note": "Sale BTP GREEN-APR45 (IT0005438004) qty 6000",
    }


def test_valid_against_real_pdf_text(mps_sale, mps_pdf_bytes):
    from core.extract import pdf_text
    v = check(mps_sale, pdf_text(mps_pdf_bytes))
    assert v.errors == [] and v.warnings == []


def test_numbers_in_text_reads_italian_and_english():
    n = numbers_in_text("totale 4.002,890 e 1,234.56 e 66,430000")
    assert Decimal("4002.89") in n and Decimal("1234.56") in n and Decimal("66.43") in n


def test_numbers_in_text_reads_space_and_apostrophe_thousands():
    n = numbers_in_text("brut 21 372,00 net 21\u00a0477,48 et 1'234.50")
    assert Decimal("21372.00") in n and Decimal("21477.48") in n and Decimal("1234.50") in n


def test_arithmetic_mismatch_is_an_error():
    bad = ExtractedTrade.model_validate({**MPS_SALE, "stated_net_amount": 4100.00})
    assert [e.code for e in check(bad).errors] == ["net_mismatch"]


def test_hallucinated_figure_not_in_document_is_an_error(mps_pdf_bytes):
    from core.extract import pdf_text
    fake = ExtractedTrade.model_validate({**MPS_SALE, "principal_amount": 3985.90,
                                          "stated_net_amount": 4002.99})
    errs = check(fake, pdf_text(mps_pdf_bytes)).errors
    assert any(e.code == "figure_not_in_document" and "3985.90" in e.message for e in errs)


def test_buy_adds_costs():
    buy = ExtractedTrade.model_validate({
        "operation": "buy", "principal_amount": 1000, "fee_items": [{"label": "c", "amount": 5}],
        "stated_net_amount": 1005, "settlement_date": "2026-01-02"})
    assert check(buy).ok and to_row(buy)["type"] == "deposit"
    assert compute(buy).net == Decimal("1005.00")


def test_other_document_is_rejected():
    other = ExtractedTrade.model_validate({"operation": "other", "principal_amount": 0,
                                           "stated_net_amount": 0})
    assert [e.code for e in check(other).errors] == ["not_a_trade"]


def test_other_currency_is_not_flagged_but_bad_isin_is():
    t = ExtractedTrade.model_validate({**MPS_SALE, "currency": "USD", "isin": "XX123"})
    w = check(t).warnings
    assert {i.code for i in w} == {"invalid_isin", "no_text_layer"}


def test_model_warnings_are_passed_through_as_notes():
    t = ExtractedTrade.model_validate({**MPS_SALE, "warnings": ["Value date is hard to read."]})
    notes = [i.message for i in check(t).warnings if i.code == "model_note"]
    assert notes == ["Value date is hard to read."]


def test_model_notes_are_capped():
    long = "Mario Rossi, Via Roma 1, conto 000123456789 " * 10
    t = ExtractedTrade.model_validate({**MPS_SALE, "warnings": [long, "a", "b", "c", "d"]})
    notes = [i.message for i in check(t).warnings if i.code == "model_note"]
    assert len(notes) == 3
    assert len(notes[0]) == 120 and notes[0].endswith("…")


def test_tool_schema_has_plain_numbers_and_required_fields():
    s = tool_schema()
    props = s["properties"]
    assert props["principal_amount"]["type"] == "number"
    assert "anyOf" not in props["principal_amount"]
    assert {"operation", "principal_amount", "stated_net_amount"} <= set(s["required"])


# Bond purchase with accrued interest: the substitute tax on the accrued interest is a CREDIT
# to the buyer (printed '-'), commissions a charge (printed '+'). Figures from a real MPS notice.
BTP_BUY = {
    "operation": "buy", "instrument_name": "BTP 3.0 OTT 2029", "isin": "IT0005611055",
    "quantity": 4000, "unit_price": 101.379, "trade_date": "2025-12-18",
    "settlement_date": "2025-12-22", "principal_amount": 4055.16, "accrued_interest": 27.03,
    "fee_items": [{"label": "commissioni totali", "amount": 3.04, "sign": "+"}],
    "tax_items": [
        {"label": "imposta sostit. su rateo interessi", "amount": 3.38, "sign": "-"},
        {"label": "imposta sostit. su scarto di emissione", "amount": 0.33, "sign": "-"},
    ],
    "stated_net_amount": 4081.52,
}


def test_bond_purchase_tax_credit_is_negative_tax():
    t = ExtractedTrade.model_validate(BTP_BUY)
    assert check(t).ok
    row = to_row(t)
    assert (row["type"], row["amount"], row["fees"], row["tax"]) == ("deposit", 4082.19, 3.04, -3.71)


def test_unsigned_items_are_assumed_to_be_charges():
    unsigned = {**BTP_BUY, "fee_items": [{"label": "c", "amount": 3.04}],
                "tax_items": [{"label": "t", "amount": 3.71}]}
    assert [e.code for e in check(ExtractedTrade.model_validate(unsigned)).errors] == ["net_mismatch"]


def test_sale_with_signed_items_matches_unsigned():
    signed = {**MPS_SALE,
              "fee_items": [{**i, "sign": "-"} for i in MPS_SALE["fee_items"]],
              "tax_items": [{**i, "sign": "-"} for i in MPS_SALE["tax_items"]]}
    assert to_row(ExtractedTrade.model_validate(signed)) == to_row(ExtractedTrade.model_validate(MPS_SALE))


def test_coupon_is_a_withdrawal_of_the_gross_with_tax():
    coupon = ExtractedTrade.model_validate({
        "operation": "coupon", "instrument_name": "BTP GREEN-APR45", "isin": "IT0005438004",
        "quantity": 6000, "unit_price": 0, "settlement_date": "2024-10-30",
        "principal_amount": 0, "accrued_interest": 45.00,
        "tax_items": [{"label": "imposta sostit. su rateo interessi", "amount": 5.63, "sign": "-"}],
        "stated_net_amount": 39.37})
    assert check(coupon).ok
    assert to_row(coupon) == {"date": "2024-10-30", "type": "withdraw", "amount": 45.0, "fees": 0.0,
                              "tax": 5.63, "note": "Coupon BTP GREEN-APR45 (IT0005438004) qty 6000"}


def test_dividend_paid_before_the_notice_date_is_fine():
    div = ExtractedTrade.model_validate({
        "operation": "dividend", "quantity": 54, "unit_price": 0.176742,
        "trade_date": "2024-11-20", "settlement_date": "2024-11-19", "principal_amount": 9.54,
        "tax_items": [{"label": "controvalore rit. fiscale Italia", "amount": 1.21, "sign": "-"}],
        "stated_net_amount": 8.33})
    assert check(div).ok and to_row(div)["type"] == "withdraw"


def test_multi_fill_detail_is_rejected():
    detail = ExtractedTrade.model_validate({**MPS_SALE, "multi_fill_detail": True})
    assert [e.code for e in check(detail).errors] == ["multi_fill_detail"]


def test_blank_strings_from_the_model_count_as_missing():
    t = ExtractedTrade.model_validate({**MPS_SALE, "trade_date": "", "isin": " ",
                                       "fee_items": [{"label": "c", "amount": 2.99, "sign": ""}]})
    assert t.trade_date is None and t.isin is None and t.fee_items[0].sign is None


# A synthetic statement: one ETF and one bond (price and accrued interest per 100 nominal).
STATEMENT = {
    "statement_date": "2025-12-31",
    "positions": [
        {"name": "SOME ETF", "code": "1234567", "quantity": 10, "market_price": 100.5,
         "value": 1005.00},
        {"name": "SOME BOND", "code": "7654321", "quantity": 2000, "market_price": 99.5,
         "accrued_rate": 0.5, "value": 2000.00},
    ],
    "stated_total": 3005.00,
}


def test_statement_checks_pass_and_offer_whole_account_or_one_position():
    from core.ledger import to_valuations
    from core.models import ExtractedStatement
    from core.validate import check_statement
    s = ExtractedStatement.model_validate(STATEMENT)
    v = check_statement(s)
    assert v.ok and [w.code for w in v.warnings] == ["no_text_layer"]
    assert to_valuations(s) == {"date": "2025-12-31", "options": [
        {"label": "Whole account", "value": 3005.0, "note": "Statement: whole account"},
        {"label": "SOME ETF (1234567)", "value": 1005.0, "note": "Statement: SOME ETF"},
        {"label": "SOME BOND (7654321)", "value": 2000.0, "note": "Statement: SOME BOND"},
    ]}


def test_statement_total_and_position_mismatches():
    from core.models import ExtractedStatement
    from core.validate import check_statement
    bad = {**STATEMENT, "stated_total": 3100.00,
           "positions": [{**STATEMENT["positions"][0], "value": 1050.00}, STATEMENT["positions"][1]]}
    v = check_statement(ExtractedStatement.model_validate(bad))
    assert [e.code for e in v.errors] == ["statement_total_mismatch"]
    assert "position_value_mismatch" in [w.code for w in v.warnings]


ENGLISH_NOTICE = """TRADE CONFIRMATION
Sold 1,000 shares ACME at USD 1,234.567
Gross amount 1,234,567.00
Commission 12.50
Net amount 1,234,554.50
Settlement date 2025-08-07
""" + "Standard terms and conditions apply to this confirmation. " * 4


def test_english_format_figures_are_grounded():
    sale = ExtractedTrade.model_validate({
        "operation": "sell", "principal_amount": 1234567.00,
        "fee_items": [{"label": "Commission", "amount": 12.50}],
        "stated_net_amount": 1234554.50, "settlement_date": "2025-08-07",
    })
    v = check(sale, ENGLISH_NOTICE)
    assert v.errors == []


def test_misread_english_decimal_is_caught():
    # 1,234,567.00 read with a shifted separator (123456.70) is not in the document.
    wrong = ExtractedTrade.model_validate({
        "operation": "sell", "principal_amount": 123456.70,
        "stated_net_amount": 123456.70, "settlement_date": "2025-08-07",
    })
    errs = check(wrong, ENGLISH_NOTICE).errors
    assert any(e.code == "figure_not_in_document" for e in errs)


def test_cusip_is_accepted_but_malformed_isin_still_warns():
    base = {"operation": "buy", "principal_amount": 100, "stated_net_amount": 100,
            "settlement_date": "2025-08-07"}
    codes = lambda isin: [w.code for w in check(ExtractedTrade.model_validate({**base, "isin": isin})).warnings]
    assert "invalid_isin" not in codes("037833100")
    assert "invalid_isin" in codes("IT000543800")  # 11 chars
    assert "invalid_isin" in codes("IT000543800X")  # 12 chars, bad check digit shape
