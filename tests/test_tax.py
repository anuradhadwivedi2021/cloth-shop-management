import random

import pytest

from core.tax import (
    DEFAULT_TAX_RULES,
    TaxRuleError,
    calculate_bill,
    ensure_default_tax_rules,
    find_rate_rule,
    load_tax_rules,
)
from db.connection import open_db

TODAY = "2026-11-01"


def _default_rules():
    return [
        {
            "id": i,
            "hsn_prefix": prefix,
            "rate_percent": rate,
            "price_ceiling_paise": ceiling,
            "valid_from": valid_from,
            "valid_to": None,
            "note": note,
        }
        for i, (prefix, rate, ceiling, valid_from, note) in enumerate(DEFAULT_TAX_RULES, start=1)
    ]


RULES = _default_rules()


def _line(price, qty=1, hsn="6205", **extra):
    return {"qty": qty, "unit_price_paise": price, "hsn": hsn, **extra}


def _bill(lines, **kw):
    kw.setdefault("on_date", TODAY)
    kw.setdefault("rules", RULES)
    return calculate_bill(lines, **kw)


def _rate(price, *, include_tax=True, **kw):
    return _bill([_line(price)], prices_include_tax=include_tax, **kw)["lines"][0]["rate_percent"]


# ---------- the worked example from the SRS (must match to the paisa) ----------

def test_srs_worked_bill_example():
    bill = _bill([_line(105000, qty=2, hsn="6205"), _line(413000, qty=1, hsn="6202")])
    shirt, blazer = bill["lines"]
    assert (shirt["rate_percent"], shirt["taxable_paise"], shirt["tax_paise"]) == (5.0, 200000, 10000)
    assert (blazer["rate_percent"], blazer["taxable_paise"], blazer["tax_paise"]) == (18.0, 350000, 63000)
    assert bill["taxable_paise"] == 550000
    assert bill["tax_paise"] == 73000
    assert bill["cgst_paise"] == 36500 and bill["sgst_paise"] == 36500 and bill["igst_paise"] == 0
    assert bill["total_paise"] == 623000
    assert bill["round_off_paise"] == 0


# ---------- the Rs 2,500 slab ----------

def test_inclusive_slab_boundary():
    # 5% applies while price / 1.05 <= 2,500.00, i.e. price <= 2,625.00
    assert _rate(262500) == 5.0
    assert _rate(262501) == 18.0


def test_srs_examples_2600_and_2700_inclusive():
    b = _bill([_line(260000)])["lines"][0]
    assert (b["rate_percent"], b["taxable_paise"], b["tax_paise"]) == (5.0, 247619, 12381)
    b = _bill([_line(270000)])["lines"][0]
    assert (b["rate_percent"], b["taxable_paise"], b["tax_paise"]) == (18.0, 228814, 41186)


def test_exclusive_slab_boundary():
    # the SRS test prices Rs 2,499 / 2,500 / 2,501 as tax-exclusive prices
    assert _rate(249900, include_tax=False) == 5.0
    assert _rate(250000, include_tax=False) == 5.0
    assert _rate(250100, include_tax=False) == 18.0


def test_exclusive_prices_add_tax_on_top():
    line = _bill([_line(100000)], prices_include_tax=False)["lines"][0]
    assert (line["taxable_paise"], line["tax_paise"], line["line_total_paise"]) == (100000, 5000, 105000)


def test_rate_follows_selling_price_not_purchase_price():
    # purchase cost never enters the calculation; only the price on the bill does
    assert _rate(300000) == 18.0


def test_rate_is_judged_per_piece_not_per_line():
    # 3 pieces at Rs 2,000 each is Rs 6,000 on the line but still 5% per piece
    line = _bill([_line(200000, qty=3)])["lines"][0]
    assert line["rate_percent"] == 5.0


def test_discount_can_move_a_piece_into_the_lower_slab():
    full = _bill([_line(270000)])["lines"][0]
    discounted = _bill([_line(270000, discount_paise=10000)])["lines"][0]
    assert full["rate_percent"] == 18.0
    assert discounted["rate_percent"] == 5.0
    assert discounted["net_paise"] == 260000


# ---------- rule table: dates, prefixes, overrides ----------

def _old_and_new_rules():
    old = [
        {"id": 100, "hsn_prefix": "61", "rate_percent": 5, "price_ceiling_paise": 100000,
         "valid_from": "2017-07-01", "valid_to": "2025-09-21"},
        {"id": 101, "hsn_prefix": "61", "rate_percent": 12, "price_ceiling_paise": None,
         "valid_from": "2017-07-01", "valid_to": "2025-09-21"},
    ]
    return old + RULES


def test_bill_uses_the_rule_valid_on_its_date():
    rules = _old_and_new_rules()
    old = _bill([_line(150000, hsn="6109")], rules=rules, on_date="2025-09-01", prices_include_tax=False)
    new = _bill([_line(150000, hsn="6109")], rules=rules, on_date="2025-09-22", prices_include_tax=False)
    assert (old["lines"][0]["rate_percent"], old["lines"][0]["tax_rule_id"]) == (12.0, 101)
    assert new["lines"][0]["rate_percent"] == 5.0


def test_longest_hsn_prefix_wins():
    rules = [
        {"id": 1, "hsn_prefix": "61", "rate_percent": 5, "price_ceiling_paise": None, "valid_from": "2020-01-01"},
        {"id": 2, "hsn_prefix": "6101", "rate_percent": 12, "price_ceiling_paise": None, "valid_from": "2020-01-01"},
    ]
    assert find_rate_rule(rules, "610110", 100000, 1, on_date=TODAY, prices_include_tax=True)["id"] == 2
    assert find_rate_rule(rules, "6109", 100000, 1, on_date=TODAY, prices_include_tax=True)["id"] == 1


def test_owner_confirmed_rate_overrides_the_rule_table():
    line = _bill([_line(112000, hsn=None, rate_percent=12)])["lines"][0]
    assert (line["rate_percent"], line["taxable_paise"], line["tax_paise"]) == (12.0, 100000, 12000)
    assert line["tax_rule_id"] is None


def test_missing_rule_and_no_override_is_an_error():
    with pytest.raises(TaxRuleError):
        _bill([_line(100000, hsn="9999")])
    with pytest.raises(TaxRuleError):
        _bill([_line(100000, hsn="")])


# ---------- tax modes ----------

def test_composition_and_unregistered_show_no_tax():
    cart = [_line(105000, qty=2, hsn="6205"), _line(413000, qty=1, hsn="6202")]
    for mode in ("composition", "unregistered"):
        bill = _bill(cart, mode=mode)
        assert bill["tax_paise"] == 0
        assert all(l["rate_percent"] == 0 and l["tax_paise"] == 0 for l in bill["lines"])
        assert bill["total_paise"] == 623000


def test_composition_needs_no_hsn_or_rules():
    bill = calculate_bill([{"qty": 1, "unit_price_paise": 50000}], on_date=TODAY, mode="composition")
    assert bill["total_paise"] == 50000


def test_inter_state_bill_uses_igst_only():
    bill = _bill([_line(105000, qty=2), _line(413000)], intra_state=False)
    assert bill["igst_paise"] == 73000
    assert bill["cgst_paise"] == 0 and bill["sgst_paise"] == 0


def test_odd_tax_paisa_goes_to_sgst():
    line = _bill([_line(10050)], prices_include_tax=False)["lines"][0]  # tax 502.5 -> 503
    assert line["tax_paise"] == 503
    assert (line["cgst_paise"], line["sgst_paise"]) == (251, 252)


# ---------- rounding ----------

def test_exclusive_tax_rounds_half_up_and_total_to_rupee():
    bill = _bill([_line(10050)], prices_include_tax=False)
    assert bill["before_round_paise"] == 10553
    assert bill["total_paise"] == 10600
    assert bill["round_off_paise"] == 47


def test_inclusive_round_off_half_goes_up():
    bill = _bill([_line(10050)])
    assert bill["total_paise"] == 10100
    assert bill["round_off_paise"] == 50


def test_round_off_can_be_negative():
    bill = _bill([_line(10020)])
    assert bill["total_paise"] == 10000
    assert bill["round_off_paise"] == -20


# ---------- discounts ----------

def test_item_discount_reduces_taxable_value():
    line = _bill([_line(105000, qty=2, discount_paise=10500)])["lines"][0]
    assert line["net_paise"] == 199500
    assert line["taxable_paise"] == 190000
    assert line["tax_paise"] == 9500


def test_bill_discount_is_spread_in_proportion_and_adds_up_exactly():
    bill = _bill([_line(100000), _line(300000)], bill_discount_paise=10001)
    shares = [l["bill_discount_share_paise"] for l in bill["lines"]]
    assert shares == [2500, 7501]
    assert sum(shares) == 10001
    assert [l["net_paise"] for l in bill["lines"]] == [97500, 292499]


def test_discount_larger_than_amount_is_rejected():
    with pytest.raises(ValueError):
        _bill([_line(1000, discount_paise=1001)])
    with pytest.raises(ValueError):
        _bill([_line(1000)], bill_discount_paise=1001)


# ---------- validation ----------

@pytest.mark.parametrize(
    "lines, kw",
    [
        ([], {}),
        ([_line(1000, qty=0)], {}),
        ([_line(1000, qty=1.5)], {}),
        ([_line(-5)], {}),
        ([_line(1000, discount_paise=-1)], {}),
        ([_line(1000)], {"bill_discount_paise": -1}),
        ([_line(1000)], {"mode": "gst-free"}),
        ([_line(1000)], {"on_date": "01/11/2026"}),
        ([_line(1000, rate_percent=-5)], {}),
    ],
)
def test_invalid_input_is_rejected(lines, kw):
    with pytest.raises(ValueError):
        _bill(lines, **kw)


# ---------- invariants on random carts ----------

def test_random_carts_always_add_up_to_the_paisa():
    rng = random.Random(2026)
    for _ in range(300):
        include = rng.choice([True, False])
        intra = rng.choice([True, False])
        cart = []
        for _ in range(rng.randint(1, 5)):
            qty = rng.randint(1, 5)
            price = rng.randint(100, 800000)
            cart.append(_line(price, qty=qty, hsn=rng.choice(["6105", "6204", "6302"]),
                              discount_paise=rng.randint(0, qty * price // 2)))
        net_sum = sum(l["qty"] * l["unit_price_paise"] - l["discount_paise"] for l in cart)
        bill_discount = rng.randint(0, net_sum // 2)
        bill = _bill(cart, prices_include_tax=include, intra_state=intra, bill_discount_paise=bill_discount)

        assert sum(l["bill_discount_share_paise"] for l in bill["lines"]) == bill_discount
        for l in bill["lines"]:
            assert l["cgst_paise"] + l["sgst_paise"] + l["igst_paise"] == l["tax_paise"]
            if include:
                assert l["taxable_paise"] + l["tax_paise"] == l["net_paise"] == l["line_total_paise"]
            else:
                assert l["taxable_paise"] == l["net_paise"]
                assert l["taxable_paise"] + l["tax_paise"] == l["line_total_paise"]
        assert bill["before_round_paise"] == sum(l["line_total_paise"] for l in bill["lines"])
        assert bill["total_paise"] == bill["before_round_paise"] + bill["round_off_paise"]
        assert bill["total_paise"] % 100 == 0
        assert -50 < bill["round_off_paise"] <= 50


# ---------- rule table stored in the database ----------

@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    yield c
    c.close()


def test_default_rules_are_added_once(conn):
    assert ensure_default_tax_rules(conn) == len(DEFAULT_TAX_RULES) == 6
    assert ensure_default_tax_rules(conn) == 0
    assert len(load_tax_rules(conn)) == 6


def test_bill_can_be_calculated_from_the_database_rules(conn):
    ensure_default_tax_rules(conn)
    bill = calculate_bill(
        [_line(105000, qty=2, hsn="6205"), _line(413000, hsn="6202")],
        on_date=TODAY,
        rules=load_tax_rules(conn),
    )
    assert bill["tax_paise"] == 73000
    assert bill["total_paise"] == 623000