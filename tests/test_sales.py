from types import SimpleNamespace

import pytest

import core.sales as sales_module
from core.audit import verify_chain
from core.numbering import next_number
from core.products import create_product, generate_variants
from core.sales import (
    create_sale,
    discard_held_bill,
    find_sale_by_invoice_no,
    get_sale,
    hold_bill,
    list_held_bills,
    reprint_sale,
    resume_held_bill,
)
from core.settings import set_setting
from core.stock import InsufficientStockError, add_movement, get_stock
from core.tax import TaxRuleError, ensure_default_tax_rules
from db.connection import open_db, transaction

DAY = "2026-11-01"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)    # Rs 1,050
    blazer = create_product(conn, "Blazer", hsn="6202", default_price_paise=413000)  # Rs 4,130
    m_blue, l_blue = generate_variants(conn, shirt, ["M", "L"], ["Blue"])
    (l_black,) = generate_variants(conn, blazer, ["L"], ["Black"])
    with transaction(conn):
        for v in (m_blue, l_blue, l_black):
            add_movement(conn, v, 10, "opening stock")
    yield SimpleNamespace(conn=conn, user=1, shirt=shirt, blazer=blazer,
                          m_blue=m_blue, l_blue=l_blue, l_black=l_black)
    conn.close()


def _cart(s):
    """The SRS example: 2 shirts + 1 blazer = Rs 6,230."""
    return [{"variant_id": s.m_blue, "qty": 2}, {"variant_id": s.l_black, "qty": 1}]


def _cash(amount=623000):
    return [{"mode": "cash", "amount_paise": amount}]


def _sell(s, cart=None, payments=None, **kw):
    kw.setdefault("on_date", DAY)
    return create_sale(
        s.conn, cart if cart is not None else _cart(s),
        payments if payments is not None else _cash(), user_id=s.user, **kw,
    )


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _nothing_saved(s):
    assert _count(s.conn, "sales") == 0
    assert _count(s.conn, "sale_lines") == 0
    assert _count(s.conn, "payments") == 0
    assert _count(s.conn, "number_series") == 0
    assert _count(s.conn, "audit_log") == 0
    assert [get_stock(s.conn, v) for v in (s.m_blue, s.l_blue, s.l_black)] == [10, 10, 10]


# ---------- saving a bill ----------

def test_bill_is_saved_with_exact_srs_figures(shop):
    sid = _sell(shop)
    data = get_sale(shop.conn, sid)
    sale = data["sale"]
    assert sale["invoice_no"] == "INV/26-27/000001"
    assert (sale["subtotal_paise"], sale["taxable_paise"], sale["tax_paise"]) == (623000, 550000, 73000)
    assert (sale["round_off_paise"], sale["total_paise"]) == (0, 623000)
    assert sale["tax_mode"] == "regular" and sale["status"] == "completed"

    shirt, blazer = data["lines"]
    assert (shirt["product_name"], shirt["size"], shirt["colour"], shirt["qty"]) == ("Shirt", "M", "Blue", 2)
    assert (shirt["taxable_paise"], shirt["tax_paise"], shirt["tax_rate_percent"]) == (200000, 10000, 5.0)
    assert (blazer["taxable_paise"], blazer["tax_paise"], blazer["tax_rate_percent"]) == (350000, 63000, 18.0)
    assert (blazer["cgst_paise"], blazer["sgst_paise"]) == (31500, 31500)
    assert shirt["tax_rule_id"] is not None


def test_stock_goes_down_exactly_once_per_variant(shop):
    sid = _sell(shop)
    assert get_stock(shop.conn, shop.m_blue) == 8
    assert get_stock(shop.conn, shop.l_black) == 9
    assert get_stock(shop.conn, shop.l_blue) == 10  # untouched variant
    moves = shop.conn.execute(
        "SELECT doc_type, doc_id, qty_change FROM stock_movements WHERE reason = 'sale' ORDER BY id"
    ).fetchall()
    assert [(m["doc_type"], m["doc_id"], m["qty_change"]) for m in moves] == [("sale", sid, -2), ("sale", sid, -1)]


def test_payment_is_recorded_against_the_bill(shop):
    sid = _sell(shop)
    data = get_sale(shop.conn, sid)
    assert [(p["mode"], p["amount_paise"]) for p in data["payments"]] == [("cash", 623000)]
    assert data["paid_paise"] == 623000 and data["due_paise"] == 0 and data["is_fully_paid"]


def test_salesperson_defaults_to_the_user_and_sale_is_audited(shop):
    sid = _sell(shop)
    assert get_sale(shop.conn, sid)["sale"]["salesperson_id"] == shop.user
    entry = shop.conn.execute("SELECT * FROM audit_log").fetchone()
    assert (entry["action"], entry["entity"], entry["entity_id"]) == ("sale_created", "sale", sid)
    assert verify_chain(shop.conn) == (True, None)


def test_invoice_numbers_are_sequential_and_never_reused(shop):
    first = get_sale(shop.conn, _sell(shop))["sale"]["invoice_no"]
    second = get_sale(shop.conn, _sell(shop))["sale"]["invoice_no"]
    assert (first, second) == ("INV/26-27/000001", "INV/26-27/000002")


def test_find_sale_by_invoice_number(shop):
    sid = _sell(shop)
    assert find_sale_by_invoice_no(shop.conn, " inv/26-27/000001 ") == sid
    assert find_sale_by_invoice_no(shop.conn, "INV/26-27/009999") is None


# ---------- stock rules ----------

def test_not_enough_stock_blocks_the_sale_and_saves_nothing(shop):
    with pytest.raises(InsufficientStockError) as err:
        _sell(shop, [{"variant_id": shop.m_blue, "qty": 11}], _cash(9_999_900))
    assert "Shirt M Blue" in str(err.value)
    _nothing_saved(shop)


def test_one_short_line_rolls_back_the_whole_bill(shop):
    cart = [{"variant_id": shop.m_blue, "qty": 2}, {"variant_id": shop.l_black, "qty": 11}]
    with pytest.raises(InsufficientStockError):
        _sell(shop, cart, _cash(9_999_900))
    _nothing_saved(shop)  # the good first line was not sold either


def test_same_variant_on_two_lines_is_checked_together(shop):
    cart = [{"variant_id": shop.m_blue, "qty": 6}, {"variant_id": shop.m_blue, "qty": 6}]
    with pytest.raises(InsufficientStockError):
        _sell(shop, cart, _cash(9_999_900))
    _nothing_saved(shop)


def test_negative_stock_is_allowed_only_when_the_shop_setting_says_so(shop):
    set_setting(shop.conn, "allow_negative_stock", 1)
    _sell(shop, [{"variant_id": shop.m_blue, "qty": 12}], _cash(1260000))
    assert get_stock(shop.conn, shop.m_blue) == -2


def test_crash_while_saving_rolls_everything_back(shop, monkeypatch):
    real = sales_module.add_movement
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(sales_module, "add_movement", flaky)
    with pytest.raises(RuntimeError):
        _sell(shop)
    _nothing_saved(shop)

    monkeypatch.setattr(sales_module, "add_movement", real)
    sid = _sell(shop)  # retry works and gets invoice number 1 (nothing was consumed)
    assert get_sale(shop.conn, sid)["sale"]["invoice_no"].endswith("000001")


# ---------- double click / retry ----------

def test_same_idempotency_key_never_creates_a_second_bill(shop):
    first = _sell(shop, idempotency_key="attempt-1")
    second = _sell(shop, idempotency_key="attempt-1")
    assert first == second
    assert _count(shop.conn, "sales") == 1
    assert _count(shop.conn, "payments") == 1
    assert get_stock(shop.conn, shop.m_blue) == 8  # reduced once, not twice
    assert shop.conn.execute("SELECT last_number FROM number_series").fetchone()[0] == 1


def test_different_keys_make_different_bills(shop):
    a = _sell(shop, idempotency_key="a")
    b = _sell(shop, idempotency_key="b")
    assert a != b and _count(shop.conn, "sales") == 2


# ---------- payments ----------

def test_split_payment_records_each_mode(shop):
    pays = [{"mode": "cash", "amount_paise": 300000},
            {"mode": "upi", "amount_paise": 323000, "reference": "UTR123"}]
    data = get_sale(shop.conn, _sell(shop, payments=pays))
    assert [(p["mode"], p["amount_paise"], p["reference"]) for p in data["payments"]] == [
        ("cash", 300000, None), ("upi", 323000, "UTR123")]
    assert data["is_fully_paid"]


def test_cash_change_is_given_and_only_the_net_cash_is_recorded(shop):
    data = get_sale(shop.conn, _sell(shop, payments=_cash(700000)))
    assert data["sale"]["tendered_paise"] == 700000
    assert data["sale"]["change_paise"] == 77000
    assert [p["amount_paise"] for p in data["payments"]] == [623000]
    assert data["is_fully_paid"]


def test_change_comes_out_of_cash_in_a_split_payment(shop):
    pays = [{"mode": "upi", "amount_paise": 400000}, {"mode": "cash", "amount_paise": 300000}]
    data = get_sale(shop.conn, _sell(shop, payments=pays))
    assert data["sale"]["change_paise"] == 77000
    assert [(p["mode"], p["amount_paise"]) for p in data["payments"]] == [("upi", 400000), ("cash", 223000)]


def test_overpaying_by_upi_is_refused(shop):
    with pytest.raises(ValueError, match="Change can only be given from cash"):
        _sell(shop, payments=[{"mode": "upi", "amount_paise": 700000}])
    _nothing_saved(shop)


def test_short_payment_is_refused_when_credit_sales_are_off(shop):
    with pytest.raises(ValueError, match="short by Rs 4,230.00"):
        _sell(shop, payments=_cash(200000))
    with pytest.raises(ValueError):
        _sell(shop, payments=[])
    _nothing_saved(shop)


def test_credit_sale_records_the_amount_due(shop):
    set_setting(shop.conn, "credit_sales_enabled", 1)
    shop.conn.execute("INSERT INTO customers(name, phone) VALUES ('Meena', '9876543210')")
    data = get_sale(shop.conn, _sell(shop, payments=_cash(200000), customer_id=1))
    assert data["paid_paise"] == 200000
    assert data["due_paise"] == 423000
    assert data["is_fully_paid"] is False


def test_credit_sale_needs_a_customer(shop):
    set_setting(shop.conn, "credit_sales_enabled", 1)
    with pytest.raises(ValueError, match="Select a customer"):
        _sell(shop, payments=_cash(200000))
    _nothing_saved(shop)


@pytest.mark.parametrize("mode", ["store_credit", "voucher", "loyalty", "gold"])
def test_payment_modes_without_a_ledger_are_refused(shop, mode):
    with pytest.raises(ValueError, match="not available"):
        _sell(shop, payments=[{"mode": mode, "amount_paise": 623000}])
    _nothing_saved(shop)


def test_zero_or_fractional_payment_is_refused(shop):
    for bad in (0, -5, 100.5):
        with pytest.raises(ValueError):
            _sell(shop, payments=[{"mode": "cash", "amount_paise": bad}])


# ---------- tax modes and discounts ----------

def test_composition_bill_is_a_bill_of_supply_with_no_tax(shop):
    data = get_sale(shop.conn, _sell(shop, mode="composition"))
    assert data["sale"]["invoice_no"] == "BOS/26-27/000001"
    assert data["sale"]["tax_paise"] == 0 and data["sale"]["total_paise"] == 623000
    assert all(l["tax_paise"] == 0 and l["tax_rate_percent"] == 0 for l in data["lines"])


def test_unregistered_bill_uses_its_own_series(shop):
    data = get_sale(shop.conn, _sell(shop, mode="unregistered"))
    assert data["sale"]["invoice_no"] == "BILL/26-27/000001"


def test_each_series_counts_separately(shop):
    _sell(shop)
    assert get_sale(shop.conn, _sell(shop, mode="composition"))["sale"]["number"] == 1
    assert get_sale(shop.conn, _sell(shop))["sale"]["number"] == 2


def test_inter_state_bill_shows_igst(shop):
    data = get_sale(shop.conn, _sell(shop, intra_state=False))
    assert data["sale"]["intra_state"] == 0
    assert [(l["cgst_paise"], l["sgst_paise"], l["igst_paise"]) for l in data["lines"]] == [
        (0, 0, 10000), (0, 0, 63000)]


def test_bill_discount_is_saved_on_the_lines_and_adds_up(shop):
    data = get_sale(shop.conn, _sell(shop, bill_discount_paise=10000, payments=_cash(613000)))
    assert data["sale"]["discount_paise"] == 10000
    assert sum(l["discount_paise"] for l in data["lines"]) == 10000
    assert data["sale"]["total_paise"] == 613000
    for l in data["lines"]:  # price x qty - discount = amount the line was taxed on
        assert l["unit_price_paise"] * l["qty"] - l["discount_paise"] == l["line_total_paise"]


def test_item_discount_reduces_the_taxable_value(shop):
    cart = [{"variant_id": shop.m_blue, "qty": 2, "discount_paise": 10500}]
    data = get_sale(shop.conn, _sell(shop, cart, _cash(199500)))
    line = data["lines"][0]
    assert (line["discount_paise"], line["taxable_paise"], line["tax_paise"]) == (10500, 190000, 9500)


def test_product_gst_rate_is_used_only_when_no_hsn_rule_applies(shop):
    pid = create_product(shop.conn, "Special", gst_rate_percent=12, default_price_paise=112000)
    (vid,) = generate_variants(shop.conn, pid, ["F"], ["Red"])
    with transaction(shop.conn):
        add_movement(shop.conn, vid, 5, "opening stock")
    data = get_sale(shop.conn, _sell(shop, [{"variant_id": vid, "qty": 1}], _cash(112000)))
    line = data["lines"][0]
    assert (line["tax_rate_percent"], line["taxable_paise"], line["tax_paise"]) == (12.0, 100000, 12000)


def test_no_rate_anywhere_blocks_the_sale(shop):
    pid = create_product(shop.conn, "Mystery", default_price_paise=50000)
    (vid,) = generate_variants(shop.conn, pid, ["F"], ["Red"])
    with transaction(shop.conn):
        add_movement(shop.conn, vid, 5, "opening stock")
    with pytest.raises(TaxRuleError):
        _sell(shop, [{"variant_id": vid, "qty": 1}], _cash(50000))
    assert _count(shop.conn, "sales") == 0


def test_price_override_on_a_variant_is_used(shop):
    shop.conn.execute("UPDATE variants SET price_override_paise = 99000 WHERE id = ?", (shop.m_blue,))
    data = get_sale(shop.conn, _sell(shop, [{"variant_id": shop.m_blue, "qty": 1}], _cash(99000)))
    assert data["lines"][0]["unit_price_paise"] == 99000


# ---------- invalid bills ----------

def test_inactive_or_unknown_or_unpriced_items_cannot_be_sold(shop):
    shop.conn.execute("UPDATE variants SET is_active = 0 WHERE id = ?", (shop.l_blue,))
    with pytest.raises(ValueError, match="inactive"):
        _sell(shop, [{"variant_id": shop.l_blue, "qty": 1}])
    with pytest.raises(ValueError, match="not found"):
        _sell(shop, [{"variant_id": 9999, "qty": 1}])
    shop.conn.execute("UPDATE products SET default_price_paise = 0 WHERE id = ?", (shop.shirt,))
    with pytest.raises(ValueError, match="no selling price"):
        _sell(shop, [{"variant_id": shop.m_blue, "qty": 1}])
    assert _count(shop.conn, "sales") == 0


@pytest.mark.parametrize("cart", [[], None, [{"variant_id": 1, "qty": 0}], [{"variant_id": 1, "qty": 1.5}],
                                  [{"variant_id": 1, "qty": -1}]])
def test_empty_cart_or_bad_quantity_is_refused(shop, cart):
    with pytest.raises(ValueError):
        create_sale(shop.conn, cart, _cash(), user_id=shop.user, on_date=DAY)
    _nothing_saved(shop)


def test_unknown_user_or_customer_is_refused(shop):
    with pytest.raises(ValueError, match="User"):
        create_sale(shop.conn, _cart(shop), _cash(), user_id=99, on_date=DAY)
    with pytest.raises(ValueError, match="Customer"):
        _sell(shop, customer_id=99)
    _nothing_saved(shop)


# ---------- reprint ----------

def test_reprint_does_not_create_a_sale_or_touch_stock_or_numbers(shop):
    sid = _sell(shop)
    snapshot = (_count(shop.conn, "sales"), _count(shop.conn, "payments"),
                _count(shop.conn, "stock_movements"),
                shop.conn.execute("SELECT last_number FROM number_series").fetchone()[0])

    first = reprint_sale(shop.conn, sid, user_id=shop.user)
    second = reprint_sale(shop.conn, sid, user_id=shop.user)

    assert (_count(shop.conn, "sales"), _count(shop.conn, "payments"), _count(shop.conn, "stock_movements"),
            shop.conn.execute("SELECT last_number FROM number_series").fetchone()[0]) == snapshot
    assert first["copy_label"] == "Duplicate copy" and (first["copy_number"], second["copy_number"]) == (1, 2)
    assert first["sale"]["invoice_no"] == "INV/26-27/000001"
    assert shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'sale_reprint'").fetchone()[0] == 2
    assert verify_chain(shop.conn) == (True, None)


def test_reprint_of_an_unknown_bill_is_refused(shop):
    with pytest.raises(ValueError):
        reprint_sale(shop.conn, 999, user_id=shop.user)


# ---------- hold and resume ----------

def test_hold_and_resume_a_bill(shop):
    slot = hold_bill(shop.conn, _cart(shop), user_id=shop.user, bill_discount_paise=5000, note="blue shirt lady")
    assert slot == 1
    assert list_held_bills(shop.conn) == [
        {"slot": 1, "customer_id": None, "note": "blue shirt lady",
         "created_at": list_held_bills(shop.conn)[0]["created_at"], "items": 2}]
    held = resume_held_bill(shop.conn, slot)
    assert held["cart"] == _cart(shop) and held["bill_discount_paise"] == 5000
    assert _count(shop.conn, "held_bills") == 1  # still saved until the sale completes
    assert _count(shop.conn, "stock_movements") == 3  # holding takes no stock (3 opening rows only)


def test_only_ten_bills_can_be_held_and_slots_are_reused(shop):
    for i in range(10):
        assert hold_bill(shop.conn, _cart(shop), user_id=shop.user) == i + 1
    with pytest.raises(ValueError, match="10 bills are already on hold"):
        hold_bill(shop.conn, _cart(shop), user_id=shop.user)
    discard_held_bill(shop.conn, 4)
    assert hold_bill(shop.conn, _cart(shop), user_id=shop.user) == 4


def test_three_held_bills_can_be_finished_out_of_order(shop):
    slots = [hold_bill(shop.conn, [{"variant_id": shop.m_blue, "qty": q}], user_id=shop.user) for q in (1, 2, 3)]
    for slot in (slots[2], slots[0], slots[1]):
        held = resume_held_bill(shop.conn, slot)
        total = held["cart"][0]["qty"] * 105000
        _sell(shop, held["cart"], _cash(total), held_slot=slot)
    assert list_held_bills(shop.conn) == []
    assert get_stock(shop.conn, shop.m_blue) == 10 - 6
    numbers = [r[0] for r in shop.conn.execute("SELECT number FROM sales ORDER BY id")]
    assert numbers == [1, 2, 3]


def test_failed_sale_keeps_the_held_bill(shop):
    slot = hold_bill(shop.conn, [{"variant_id": shop.m_blue, "qty": 11}], user_id=shop.user)
    with pytest.raises(InsufficientStockError):
        _sell(shop, resume_held_bill(shop.conn, slot)["cart"], _cash(1155000), held_slot=slot)
    assert len(list_held_bills(shop.conn)) == 1


def test_nothing_to_hold_and_unknown_slot_are_refused(shop):
    with pytest.raises(ValueError):
        hold_bill(shop.conn, [], user_id=shop.user)
    with pytest.raises(ValueError):
        resume_held_bill(shop.conn, 7)
    with pytest.raises(ValueError):
        discard_held_bill(shop.conn, 7)


# ---------- numbering shared with other documents ----------

def test_sale_numbers_do_not_clash_with_other_series(shop):
    with transaction(shop.conn):
        next_number(shop.conn, "PUR", "26-27", "PUR")
    assert get_sale(shop.conn, _sell(shop))["sale"]["number"] == 1
