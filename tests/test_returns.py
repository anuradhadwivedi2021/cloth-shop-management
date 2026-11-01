import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from core.audit import verify_chain
from core.products import create_product, generate_variants
from core.returns import (
    get_return,
    get_store_credit_balance,
    list_returns_for_sale,
    process_return,
    returnable_lines,
)
from core.sales import create_sale, get_sale
from core.settings import set_setting
from core.stock import InsufficientStockError, add_movement, get_stock
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

DAY = "2026-11-01"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('till', 'x', 'cashier')")
    conn.execute("INSERT INTO customers(name, phone) VALUES ('Asha', '9000000001')")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000, category="Shirts")      # Rs 1,050
    blazer = create_product(conn, "Blazer", hsn="6202", default_price_paise=413000, category="Blazers")  # Rs 4,130
    polo = create_product(conn, "Polo", hsn="6105", default_price_paise=135000, category="Shirts")       # Rs 1,350
    vest = create_product(conn, "Vest", hsn="6109", default_price_paise=85000, category="Innerwear")     # Rs 850
    (m_blue,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    (l_black,) = generate_variants(conn, blazer, ["L"], ["Black"])
    (polo_m,) = generate_variants(conn, polo, ["M"], ["Red"])
    (vest_m,) = generate_variants(conn, vest, ["M"], ["White"])
    with transaction(conn):
        for v in (m_blue, l_black, polo_m, vest_m):
            add_movement(conn, v, 10, "opening stock")
    yield SimpleNamespace(
        conn=conn, owner=1, cashier=2, customer=1,
        m_blue=m_blue, l_black=l_black, polo_m=polo_m, vest_m=vest_m,
    )
    conn.close()


# ---------- helpers ----------

def _sell(s, cart=None, payments=None, **kw):
    """Default bill = the SRS example: 2 shirts + 1 blazer = Rs 6,230. Overpaying cash gives change."""
    cart = cart if cart is not None else [
        {"variant_id": s.m_blue, "qty": 2}, {"variant_id": s.l_black, "qty": 1},
    ]
    payments = payments if payments is not None else [{"mode": "cash", "amount_paise": 10**7}]
    kw.setdefault("on_date", DAY)
    return create_sale(s.conn, cart, payments, user_id=s.owner, **kw)


def _line(s, sale_id, variant_id):
    return s.conn.execute(
        "SELECT id FROM sale_lines WHERE sale_id = ? AND variant_id = ?", (sale_id, variant_id)
    ).fetchone()["id"]


def _item(s, sale_id, variant_id, qty=1, reason="wrong_size"):
    return {"sale_line_id": _line(s, sale_id, variant_id), "qty": qty, "reason": reason}


def _ret(s, sale_id, items, **kw):
    kw.setdefault("user_id", s.owner)
    kw.setdefault("on_date", DAY)
    return process_return(s.conn, sale_id, items, **kw)


def _count(s, table):
    return s.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _check_adds_up(s, return_id):
    """The credit note total is fully accounted for: due reduced + refund + store credit + credit used."""
    r = get_return(s.conn, return_id)["return"]
    assert r["total_paise"] == (
        r["due_adjusted_paise"] + r["refund_paise"] + r["store_credit_paise"] + r["credit_applied_paise"]
    )
    return r


# ---------- basic return ----------

def test_full_return_refunds_exactly_what_was_paid(shop):
    s = shop
    sale = _sell(s)
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 2), _item(s, sale, s.l_black, 1)])
    data = get_return(s.conn, rid)
    r = _check_adds_up(s, rid)
    assert r["credit_note_no"] == "CN/26-27/000001"
    assert r["total_paise"] == 623000 and r["refund_paise"] == 623000
    assert [p["amount_paise"] for p in data["payments"]] == [-623000]
    assert data["payments"][0]["mode"] == "cash"
    assert data["payments"][0]["reference"] == "CN/26-27/000001"
    assert data["original_invoice_no"] == get_sale(s.conn, sale)["sale"]["invoice_no"]
    assert data["exchange_invoice_no"] is None
    # money in minus money out is zero
    net = s.conn.execute("SELECT SUM(amount_paise) FROM payments").fetchone()[0]
    assert net == 0


def test_partial_return_is_valued_at_the_original_bill(shop):
    s = shop
    sale = _sell(s)
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    data = get_return(s.conn, rid)
    (line,) = data["lines"]
    assert line["line_total_paise"] == 105000
    assert line["taxable_paise"] == 100000 and line["tax_paise"] == 5000  # 5% slab, tax-inclusive tag
    assert line["taxable_paise"] + line["tax_paise"] == line["line_total_paise"]
    assert data["return"]["total_paise"] == 105000


def test_price_change_after_sale_does_not_change_the_refund(shop):
    s = shop
    sale = _sell(s)
    s.conn.execute("UPDATE products SET default_price_paise = 200000 WHERE name = 'Shirt'")
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    assert get_return(s.conn, rid)["return"]["refund_paise"] == 105000


def test_return_in_two_steps_adds_up_to_the_line(shop):
    s = shop
    sale = _sell(s)
    a = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    b = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    assert get_return(s.conn, a)["return"]["total_paise"] + get_return(s.conn, b)["return"]["total_paise"] == 210000
    assert [x["credit_note_no"] for x in list_returns_for_sale(s.conn, sale)] == [
        "CN/26-27/000001", "CN/26-27/000002",
    ]


def test_returnable_lines_show_what_is_left(shop):
    s = shop
    sale = _sell(s)
    _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    by_variant = {l["variant_id"]: l for l in returnable_lines(s.conn, sale)}
    assert by_variant[s.m_blue]["qty_sold"] == 2
    assert by_variant[s.m_blue]["qty_returned"] == 1
    assert by_variant[s.m_blue]["qty_returnable"] == 1
    assert by_variant[s.l_black]["qty_returnable"] == 1


# ---------- limits and double processing ----------

def test_cannot_return_more_than_was_sold(shop):
    s = shop
    sale = _sell(s)
    with pytest.raises(ValueError, match="only 2"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 3)])
    _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    with pytest.raises(ValueError, match="only 1"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 2)])
    assert _count(s, "sale_returns") == 1


def test_same_return_cannot_be_processed_twice(shop):
    s = shop
    sale = _sell(s)
    items = [_item(s, sale, s.m_blue, 2), _item(s, sale, s.l_black, 1)]
    _ret(s, sale, items)
    with pytest.raises(ValueError, match="already returned"):
        _ret(s, sale, items)
    assert _count(s, "sale_returns") == 1
    assert get_stock(s.conn, s.m_blue, "returned_pending") == 2
    assert s.conn.execute("SELECT COUNT(*) FROM payments WHERE amount_paise < 0").fetchone()[0] == 1


def test_double_click_with_same_key_returns_the_saved_credit_note(shop):
    s = shop
    sale = _sell(s)
    first = _ret(s, sale, [_item(s, sale, s.m_blue, 1)], idempotency_key="click-1")
    again = _ret(s, sale, [_item(s, sale, s.m_blue, 1)], idempotency_key="click-1")
    assert first == again
    assert _count(s, "sale_returns") == 1
    assert get_stock(s.conn, s.m_blue, "returned_pending") == 1
    assert s.conn.execute("SELECT COUNT(*) FROM payments WHERE amount_paise < 0").fetchone()[0] == 1


def test_bad_requests_save_nothing(shop):
    s = shop
    sale = _sell(s)
    other = _sell(s, cart=[{"variant_id": s.vest_m, "qty": 1}])
    line = _item(s, sale, s.m_blue, 1)
    cases = [
        ([], "at least one"),
        ([{**line, "qty": 0}], "greater than zero"),
        ([{**line, "qty": 1.5}], "whole number"),
        ([{**line, "reason": ""}], "reason"),
        ([line, line], "twice"),
        ([_item(s, other, s.vest_m, 1)], "not on this bill"),
    ]
    for items, message in cases:
        with pytest.raises(ValueError, match=message):
            _ret(s, sale, items)
    with pytest.raises(ValueError, match="Bill not found"):
        _ret(s, 9999, [line])
    assert _count(s, "sale_returns") == 0
    assert get_stock(s.conn, s.m_blue, "returned_pending") == 0


def test_cancelled_bill_cannot_be_returned(shop):
    s = shop
    sale = _sell(s)
    s.conn.execute("UPDATE sales SET status = 'cancelled' WHERE id = ?", (sale,))
    with pytest.raises(ValueError, match="cancelled"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 1)])


def test_overall_reason_covers_every_line(shop):
    s = shop
    sale = _sell(s)
    items = [
        {"sale_line_id": _line(s, sale, s.m_blue), "qty": 1},
        {"sale_line_id": _line(s, sale, s.l_black), "qty": 1},
    ]
    rid = _ret(s, sale, items, reason="customer_preference")
    assert {l["reason"] for l in get_return(s.conn, rid)["lines"]} == {"customer_preference"}


# ---------- stock ----------

def test_returned_pieces_wait_for_a_check_and_are_not_sellable(shop):
    s = shop
    sale = _sell(s)
    assert get_stock(s.conn, s.m_blue) == 8
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 2)])
    assert get_stock(s.conn, s.m_blue) == 8                       # still not sellable
    assert get_stock(s.conn, s.m_blue, "returned_pending") == 2
    move = s.conn.execute(
        "SELECT * FROM stock_movements WHERE doc_type = 'sale_return' AND doc_id = ?", (rid,)
    ).fetchone()
    assert move["qty_change"] == 2 and move["bucket"] == "returned_pending" and move["user_id"] == s.owner


# ---------- return policy ----------

def test_return_period_blocks_late_returns(shop):
    s = shop
    sale = _sell(s)
    set_setting(s.conn, "return_window_days", 7)
    late = (date.today() + timedelta(days=30)).isoformat()
    with pytest.raises(ValueError, match="7 days"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 1)], on_date=late)
    assert _count(s, "sale_returns") == 0
    # inside the period it works, and the blocked try left no gap in the numbers
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 1)], on_date=date.today().isoformat())
    assert get_return(s.conn, rid)["return"]["number"] == 1


def test_owner_can_override_the_policy_with_a_reason_and_it_is_logged(shop):
    s = shop
    sale = _sell(s)
    set_setting(s.conn, "return_window_days", 7)
    late = (date.today() + timedelta(days=30)).isoformat()
    item = [_item(s, sale, s.m_blue, 1)]
    with pytest.raises(ValueError, match="Only the owner"):
        _ret(s, sale, item, on_date=late, override_user_id=s.cashier, override_reason="regular customer")
    with pytest.raises(ValueError, match="reason"):
        _ret(s, sale, item, on_date=late, override_user_id=s.owner)
    rid = _ret(s, sale, item, on_date=late, override_user_id=s.owner, override_reason="regular customer")
    r = get_return(s.conn, rid)["return"]
    assert r["override_user_id"] == s.owner and r["override_reason"] == "regular customer"
    entry = s.conn.execute("SELECT after_json FROM audit_log WHERE action = 'sale_return_created'").fetchone()
    assert json.loads(entry["after_json"])["policy_override_reason"] == "regular customer"


def test_final_sale_category_cannot_be_returned_without_override(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.vest_m, "qty": 1}])
    set_setting(s.conn, "final_sale_categories", "Innerwear, Socks")
    with pytest.raises(ValueError, match="final-sale"):
        _ret(s, sale, [_item(s, sale, s.vest_m, 1)])
    rid = _ret(s, sale, [_item(s, sale, s.vest_m, 1)], override_user_id=s.owner, override_reason="defect found")
    assert get_return(s.conn, rid)["return"]["override_user_id"] == s.owner


def test_quantity_limit_cannot_be_overridden(shop):
    s = shop
    sale = _sell(s)
    with pytest.raises(ValueError, match="only 2"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 5)], override_user_id=s.owner, override_reason="please")


def test_bad_policy_setting_is_reported(shop):
    s = shop
    sale = _sell(s)
    set_setting(s.conn, "return_window_days", "soon")
    with pytest.raises(ValueError, match="whole number of days"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 1)])


# ---------- refund mode ----------

def test_refund_goes_back_the_way_it_was_paid(shop):
    s = shop
    sale = _sell(s, payments=[{"mode": "cash", "amount_paise": 200000}, {"mode": "upi", "amount_paise": 423000}])
    rid = _ret(s, sale, [_item(s, sale, s.l_black, 1)])
    (pay,) = get_return(s.conn, rid)["payments"]
    assert pay["mode"] == "upi" and pay["amount_paise"] == -413000


def test_refund_mode_can_be_chosen_and_bad_modes_are_refused(shop):
    s = shop
    sale = _sell(s, payments=[{"mode": "upi", "amount_paise": 623000}])
    with pytest.raises(ValueError, match="not available"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 1)], refund_mode="bitcoin")
    assert _count(s, "sale_returns") == 0 and get_stock(s.conn, s.m_blue, "returned_pending") == 0
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 1)], refund_mode="cash")
    assert get_return(s.conn, rid)["payments"][0]["mode"] == "cash"


# ---------- credit sales ----------

def test_return_on_a_credit_sale_reduces_the_amount_due_first(shop):
    s = shop
    set_setting(s.conn, "credit_sales_enabled", 1)
    sale = _sell(s, payments=[{"mode": "cash", "amount_paise": 100000}], customer_id=s.customer)
    assert get_sale(s.conn, sale)["due_paise"] == 523000

    a = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])           # 1,050 returned, 5,230 still due
    ra = _check_adds_up(s, a)
    assert ra["due_adjusted_paise"] == 105000 and ra["refund_paise"] == 0
    assert get_return(s.conn, a)["payments"] == []

    b = _ret(s, sale, [_item(s, sale, s.l_black, 1)])          # 4,130 more
    assert _check_adds_up(s, b)["refund_paise"] == 0

    c = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])           # only 50 was still due; the rest is refunded
    rc = _check_adds_up(s, c)
    assert rc["due_adjusted_paise"] == 5000 and rc["refund_paise"] == 100000   # = what the customer paid


# ---------- store credit ----------

def test_store_credit_needs_a_customer_and_goes_to_the_ledger(shop):
    s = shop
    sale = _sell(s)  # walk-in
    item = [_item(s, sale, s.m_blue, 1)]
    with pytest.raises(ValueError, match="customer"):
        _ret(s, sale, item, outcome="store_credit")
    assert _count(s, "sale_returns") == 0
    rid = _ret(s, sale, item, outcome="store_credit", customer_id=s.customer)
    r = _check_adds_up(s, rid)
    assert r["store_credit_paise"] == 105000 and r["refund_paise"] == 0
    assert get_store_credit_balance(s.conn, s.customer) == 105000
    assert get_return(s.conn, rid)["payments"] == []          # no money left the till


def test_store_credit_uses_the_bills_customer(shop):
    s = shop
    sale = _sell(s, customer_id=s.customer)
    _ret(s, sale, [_item(s, sale, s.l_black, 1)], outcome="store_credit")
    assert get_store_credit_balance(s.conn, s.customer) == 413000


# ---------- exchanges ----------

def test_exchange_for_a_dearer_item_customer_pays_the_difference(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}])
    rid = _ret(
        s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
        exchange_cart=[{"variant_id": s.polo_m, "qty": 1}],
        payments=[{"mode": "cash", "amount_paise": 30000}],      # 1,350 - 1,050
    )
    data = get_return(s.conn, rid)
    r = _check_adds_up(s, rid)
    assert r["credit_applied_paise"] == 105000 and r["refund_paise"] == 0
    new = get_sale(s.conn, r["exchange_sale_id"])
    assert new["sale"]["total_paise"] == 135000 and new["is_fully_paid"]
    assert sorted((p["mode"], p["amount_paise"]) for p in new["payments"]) == [
        ("cash", 30000), ("store_credit", 105000),
    ]
    assert [p["reference"] for p in new["payments"] if p["mode"] == "store_credit"] == [r["credit_note_no"]]
    assert data["exchange_invoice_no"] == new["sale"]["invoice_no"]
    assert get_stock(s.conn, s.polo_m) == 9                       # replacement went out
    assert get_stock(s.conn, s.m_blue, "returned_pending") == 1   # returned piece waits for a check
    assert get_stock(s.conn, s.m_blue) == 9


def test_exchange_for_a_cheaper_item_customer_gets_the_difference_back(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}])
    rid = _ret(
        s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
        exchange_cart=[{"variant_id": s.vest_m, "qty": 1}],
    )
    r = _check_adds_up(s, rid)
    assert r["credit_applied_paise"] == 85000 and r["refund_paise"] == 20000     # 1,050 - 850
    (pay,) = get_return(s.conn, rid)["payments"]
    assert pay["mode"] == "cash" and pay["amount_paise"] == -20000
    assert get_sale(s.conn, r["exchange_sale_id"])["is_fully_paid"]


def test_exchange_surplus_can_become_store_credit(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}],
                 customer_id=s.customer)
    rid = _ret(
        s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
        exchange_cart=[{"variant_id": s.vest_m, "qty": 1}], surplus_outcome="store_credit",
    )
    r = _check_adds_up(s, rid)
    assert r["store_credit_paise"] == 20000 and r["refund_paise"] == 0
    assert get_store_credit_balance(s.conn, s.customer) == 20000


def test_exchange_at_the_same_price_needs_no_money(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}])
    rid = _ret(
        s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
        exchange_cart=[{"variant_id": s.m_blue, "qty": 1}],
    )
    r = _check_adds_up(s, rid)
    assert r["credit_applied_paise"] == 105000 and r["refund_paise"] == 0
    assert get_return(s.conn, rid)["payments"] == []


def test_exchange_that_is_underpaid_saves_nothing(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}])
    with pytest.raises(ValueError, match="short"):
        _ret(
            s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
            exchange_cart=[{"variant_id": s.polo_m, "qty": 1}],
            payments=[{"mode": "cash", "amount_paise": 20000}],  # needs 30,000
        )
    assert _count(s, "sale_returns") == 0 and _count(s, "sales") == 1
    assert get_stock(s.conn, s.polo_m) == 10 and get_stock(s.conn, s.m_blue, "returned_pending") == 0
    # numbers were released: the next valid exchange still gets CN 1 and invoice 2
    rid = _ret(
        s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
        exchange_cart=[{"variant_id": s.polo_m, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 30000}],
    )
    r = get_return(s.conn, rid)
    assert r["return"]["number"] == 1 and r["exchange_invoice_no"] == "INV/26-27/000002"


def test_exchange_with_replacement_out_of_stock_saves_nothing(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}])
    with pytest.raises(InsufficientStockError):
        _ret(
            s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
            exchange_cart=[{"variant_id": s.polo_m, "qty": 11}],
            payments=[{"mode": "cash", "amount_paise": 10**7}],
        )
    assert _count(s, "sale_returns") == 0 and _count(s, "sales") == 1
    assert get_stock(s.conn, s.m_blue, "returned_pending") == 0


def test_exchange_double_click_creates_one_credit_note_and_one_new_bill(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}])
    args = dict(
        outcome="exchange", exchange_cart=[{"variant_id": s.polo_m, "qty": 1}],
        payments=[{"mode": "cash", "amount_paise": 30000}], idempotency_key="ex-1",
    )
    first = _ret(s, sale, [_item(s, sale, s.m_blue, 1)], **args)
    again = _ret(s, sale, [_item(s, sale, s.m_blue, 1)], **args)
    assert first == again and _count(s, "sale_returns") == 1 and _count(s, "sales") == 2
    assert get_stock(s.conn, s.polo_m) == 9


def test_exchange_needs_replacement_items_and_only_exchanges_take_them(shop):
    s = shop
    sale = _sell(s)
    item = [_item(s, sale, s.m_blue, 1)]
    with pytest.raises(ValueError, match="replacement"):
        _ret(s, sale, item, outcome="exchange")
    with pytest.raises(ValueError, match="only be given"):
        _ret(s, sale, item, exchange_cart=[{"variant_id": s.polo_m, "qty": 1}])
    with pytest.raises(ValueError, match="Outcome"):
        _ret(s, sale, item, outcome="gift")
    assert _count(s, "sale_returns") == 0


def test_exchange_keeps_the_tax_mode_of_the_original_bill(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}],
                 mode="unregistered")
    rid = _ret(
        s, sale, [_item(s, sale, s.m_blue, 1)], outcome="exchange",
        exchange_cart=[{"variant_id": s.vest_m, "qty": 1}],
    )
    new = get_sale(s.conn, get_return(s.conn, rid)["return"]["exchange_sale_id"])
    assert new["sale"]["tax_mode"] == "unregistered" and new["sale"]["tax_paise"] == 0
    assert new["sale"]["invoice_no"].startswith("BILL/")


# ---------- tax modes and rounding ----------

def test_return_on_a_bill_without_tax_has_no_tax(shop):
    s = shop
    sale = _sell(s, cart=[{"variant_id": s.m_blue, "qty": 2}], mode="composition")
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    r = get_return(s.conn, rid)["return"]
    assert r["tax_paise"] == 0 and r["total_paise"] == 105000


def test_returning_a_discounted_bill_in_pieces_refunds_exactly_what_was_paid(shop):
    s = shop
    sale = _sell(
        s, cart=[{"variant_id": s.m_blue, "qty": 3}, {"variant_id": s.l_black, "qty": 1}],
        bill_discount_paise=3333,
    )
    paid = s.conn.execute(
        "SELECT SUM(amount_paise) FROM payments WHERE document_type = 'sale' AND document_id = ?", (sale,)
    ).fetchone()[0]
    ids = [_ret(s, sale, [_item(s, sale, s.m_blue, 1)]) for _ in range(3)]
    ids.append(_ret(s, sale, [_item(s, sale, s.l_black, 1)]))
    refunds = [_check_adds_up(s, i)["refund_paise"] for i in ids]
    assert all(r % 100 == 0 for r in refunds)       # whole rupees, like the bill itself
    assert sum(refunds) == paid


# ---------- safety: records, numbers, audit ----------

def test_credit_note_numbers_run_in_order_and_are_never_reused(shop):
    s = shop
    sale = _sell(s)
    numbers = []
    for qty_line in (s.m_blue, s.m_blue, s.l_black):
        rid = _ret(s, sale, [_item(s, sale, qty_line, 1)])
        numbers.append(get_return(s.conn, rid)["return"]["credit_note_no"])
    assert numbers == ["CN/26-27/000001", "CN/26-27/000002", "CN/26-27/000003"]


def test_credit_notes_cannot_be_edited_or_deleted(shop):
    s = shop
    sale = _sell(s)
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    for sql in (
        "UPDATE sale_returns SET total_paise = 1",
        "DELETE FROM sale_returns",
        "UPDATE sale_return_lines SET qty = 5",
        "DELETE FROM sale_return_lines",
        "UPDATE payments SET amount_paise = -1 WHERE amount_paise < 0",
        "DELETE FROM payments WHERE amount_paise < 0",
    ):
        with pytest.raises(sqlcipher.IntegrityError):
            s.conn.execute(sql)
    s.conn.execute("INSERT INTO store_credit_ledger(customer_id, amount_paise) VALUES (1, 100)")
    with pytest.raises(sqlcipher.IntegrityError):
        s.conn.execute("DELETE FROM store_credit_ledger")
    assert get_return(s.conn, rid)["return"]["total_paise"] == 105000


def test_original_bill_is_untouched_by_a_return(shop):
    s = shop
    sale = _sell(s)
    before = get_sale(s.conn, sale)
    _ret(s, sale, [_item(s, sale, s.m_blue, 2)])
    after = get_sale(s.conn, sale)
    assert after["sale"] == before["sale"] and after["lines"] == before["lines"]
    assert after["payments"] == before["payments"]


def test_returns_are_in_the_audit_trail(shop):
    s = shop
    sale = _sell(s)
    rid = _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    entry = s.conn.execute(
        "SELECT * FROM audit_log WHERE action = 'sale_return_created' AND entity_id = ?", (rid,)
    ).fetchone()
    assert entry["user_id"] == s.owner
    assert json.loads(entry["after_json"])["refund_paise"] == 105000
    assert verify_chain(s.conn) == (True, None)


def test_inactive_user_cannot_process_a_return(shop):
    s = shop
    sale = _sell(s)
    s.conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (s.cashier,))
    with pytest.raises(ValueError, match="not active"):
        _ret(s, sale, [_item(s, sale, s.m_blue, 1)], user_id=s.cashier)