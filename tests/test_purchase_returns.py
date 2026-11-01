from types import SimpleNamespace

import pytest

from core.audit import verify_chain
from core.products import create_product, generate_variants
from core.purchase_returns import (
    get_purchase_return, list_purchase_returns, process_purchase_return, returnable_purchase_lines,
)
from core.purchases import confirm_purchase, create_draft_purchase, create_supplier, get_purchase
from core.stock import InsufficientStockError, get_stock
from core.stock_ops import mark_missing
from db.connection import open_db

DAY = "2026-11-01"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('store', 'x', 'stock')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('till', 'x', 'cashier')")
    supplier = create_supplier(conn, "Ravi Textiles")
    pid = create_product(conn, "Shirt")
    m, l = generate_variants(conn, pid, ["M", "L"], ["Blue"])
    yield SimpleNamespace(conn=conn, owner=1, stock=2, cashier=3, supplier=supplier, m=m, l=l)
    conn.close()


def _purchase(s, *, paid=0, discount=0, confirm=True, lines=None, mode="cash"):
    """10 x M-Blue and 10 x L-Blue at Rs 500, 5% tax: Rs 10,500 before discount."""
    lines = lines or [
        {"variant_id": s.m, "qty": 10, "unit_cost_paise": 50000, "tax_rate_percent": 5},
        {"variant_id": s.l, "qty": 10, "unit_cost_paise": 50000, "tax_rate_percent": 5},
    ]
    pid = create_draft_purchase(
        s.conn, s.supplier, "SUP-1", DAY, lines, discount_paise=discount, paid_paise=paid, created_by=s.owner
    )
    if confirm:
        confirm_purchase(s.conn, pid, user_id=s.owner, payment_mode=mode)
    _, plines = get_purchase(s.conn, pid)
    return pid, [pl["id"] for pl in plines]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_return_reduces_stock_and_due(shop):
    pid, (m_line, l_line) = _purchase(shop)  # nothing paid: we owe Rs 10,500
    rid = process_purchase_return(
        shop.conn, pid, [{"purchase_line_id": m_line, "qty": 4}],
        user_id=shop.owner, reason="wrong colour", on_date=DAY,
    )
    note = get_purchase_return(shop.conn, rid)
    r = note["return"]
    assert r["debit_note_no"] == "DN/26-27/000001"
    assert (r["subtotal_paise"], r["tax_paise"], r["total_paise"]) == (200000, 10000, 210000)
    assert (r["due_reduced_paise"], r["refund_paise"], r["supplier_credit_paise"]) == (210000, 0, 0)
    assert note["payments"] == []  # no money moves, we simply owe less
    assert get_stock(shop.conn, shop.m) == 6
    assert get_stock(shop.conn, shop.l) == 10
    mv = shop.conn.execute("SELECT * FROM stock_movements WHERE doc_type = 'purchase_return'").fetchall()
    assert len(mv) == 1 and mv[0]["qty_change"] == -4 and mv[0]["doc_id"] == rid


def test_fully_paid_purchase_gets_refund_as_negative_payment(shop):
    pid, (m_line, _) = _purchase(shop, paid=1050000, mode="upi")
    rid = process_purchase_return(
        shop.conn, pid, [{"purchase_line_id": m_line, "qty": 4}], user_id=shop.owner, reason="defect", on_date=DAY,
    )
    note = get_purchase_return(shop.conn, rid)
    assert note["return"]["refund_paise"] == 210000 and note["return"]["due_reduced_paise"] == 0
    pay = note["payments"]
    assert len(pay) == 1 and pay[0]["amount_paise"] == -210000
    assert pay[0]["mode"] == "upi" and pay[0]["reference"] == "DN/26-27/000001"


def test_partly_paid_reduces_due_first_then_refunds_rest(shop):
    # total 1,050,000; paid 900,000 -> due 150,000. Returning 210,000: 150,000 off the due, 60,000 refunded.
    pid, (m_line, _) = _purchase(shop, paid=900000)
    rid = process_purchase_return(
        shop.conn, pid, [{"purchase_line_id": m_line, "qty": 4}], user_id=shop.owner, reason="defect", on_date=DAY,
    )
    r = get_purchase_return(shop.conn, rid)["return"]
    assert (r["due_reduced_paise"], r["refund_paise"]) == (150000, 60000)
    assert r["total_paise"] == r["due_reduced_paise"] + r["refund_paise"] + r["supplier_credit_paise"]


def test_credit_settlement_keeps_value_as_supplier_credit(shop):
    pid, (m_line, _) = _purchase(shop, paid=1050000)
    rid = process_purchase_return(
        shop.conn, pid, [{"purchase_line_id": m_line, "qty": 4}],
        user_id=shop.owner, reason="defect", settlement="credit", on_date=DAY,
    )
    note = get_purchase_return(shop.conn, rid)
    assert note["return"]["supplier_credit_paise"] == 210000 and note["return"]["refund_paise"] == 0
    assert note["payments"] == []


def test_invoice_discount_is_shared_and_full_return_gives_back_everything(shop):
    pid, lines = _purchase(shop, discount=52500)  # 5% invoice discount, total 9,975
    ids = []
    for qty in (4, 6):  # return all 10 of M-Blue in two steps, then all of L-Blue
        ids.append(process_purchase_return(
            shop.conn, pid, [{"purchase_line_id": lines[0], "qty": qty}], user_id=shop.owner, reason="r", on_date=DAY,
        ))
    ids.append(process_purchase_return(
        shop.conn, pid, [{"purchase_line_id": lines[1], "qty": 10}], user_id=shop.owner, reason="r", on_date=DAY,
    ))
    rows = [get_purchase_return(shop.conn, i)["return"] for i in ids]
    assert sum(r["discount_paise"] for r in rows) == 52500
    assert sum(r["subtotal_paise"] for r in rows) == 1000000
    assert sum(r["tax_paise"] for r in rows) == 50000
    # before rounding the notes add up to exactly the invoice total; each note is rounded to a rupee on its own
    assert sum(r["total_paise"] - r["round_off_paise"] for r in rows) == 997500
    assert all(r["total_paise"] % 100 == 0 for r in rows)
    assert get_stock(shop.conn, shop.m) == 0 and get_stock(shop.conn, shop.l) == 0


def test_cannot_return_more_than_bought_or_twice(shop):
    pid, (m_line, _) = _purchase(shop)
    process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 7}], user_id=shop.owner, reason="r", on_date=DAY)
    with pytest.raises(ValueError, match="only 3 can still be returned"):
        process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 4}], user_id=shop.owner, reason="r", on_date=DAY)
    assert get_stock(shop.conn, shop.m) == 3
    assert _count(shop.conn, "purchase_returns") == 1
    info = [x for x in returnable_purchase_lines(shop.conn, pid) if x["purchase_line_id"] == m_line][0]
    assert (info["qty_bought"], info["qty_returned"], info["qty_returnable"], info["sellable_now"]) == (10, 7, 3, 3)


def test_cannot_return_more_than_sellable_stock(shop):
    pid, (m_line, _) = _purchase(shop)
    mark_missing(shop.conn, shop.m, 8, "sold elsewhere / lost", user_id=shop.owner)  # only 2 left
    with pytest.raises(InsufficientStockError, match="only 2 sellable"):
        process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 3}], user_id=shop.owner, reason="r", on_date=DAY)
    assert _count(shop.conn, "purchase_returns") == 0 and get_stock(shop.conn, shop.m) == 2


def test_one_bad_line_saves_nothing(shop):
    pid, (m_line, l_line) = _purchase(shop)
    before = _count(shop.conn, "audit_log")
    with pytest.raises(ValueError):
        process_purchase_return(
            shop.conn, pid,
            [{"purchase_line_id": m_line, "qty": 2}, {"purchase_line_id": l_line, "qty": 11}],
            user_id=shop.owner, reason="r", on_date=DAY,
        )
    assert get_stock(shop.conn, shop.m) == 10
    assert _count(shop.conn, "purchase_returns") == 0 and _count(shop.conn, "audit_log") == before
    # the failed attempt did not burn a debit note number
    rid = process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.owner, reason="r", on_date=DAY)
    assert get_purchase_return(shop.conn, rid)["return"]["number"] == 1


def test_only_confirmed_purchase_can_be_returned(shop):
    pid, (m_line, _) = _purchase(shop, confirm=False)
    with pytest.raises(ValueError, match="confirmed"):
        process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.owner, reason="r", on_date=DAY)
    with pytest.raises(ValueError, match="not found"):
        process_purchase_return(shop.conn, 999, [{"purchase_line_id": 1, "qty": 1}], user_id=shop.owner, reason="r", on_date=DAY)


def test_line_from_another_purchase_is_refused(shop):
    pid1, _ = _purchase(shop)
    _, (other_line, _) = _purchase(shop)
    with pytest.raises(ValueError, match="not on this purchase"):
        process_purchase_return(shop.conn, pid1, [{"purchase_line_id": other_line, "qty": 1}], user_id=shop.owner, reason="r", on_date=DAY)


@pytest.mark.parametrize("qty", [0, -2, 1.5, "3", None])
def test_bad_quantity_refused(shop, qty):
    pid, (m_line, _) = _purchase(shop)
    with pytest.raises(ValueError, match="whole number"):
        process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": qty}], user_id=shop.owner, reason="r", on_date=DAY)


def test_reason_items_and_duplicates_validated(shop):
    pid, (m_line, _) = _purchase(shop)
    with pytest.raises(ValueError, match="reason"):
        process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.owner, reason=" ")
    with pytest.raises(ValueError, match="at least one"):
        process_purchase_return(shop.conn, pid, [], user_id=shop.owner, reason="r")
    with pytest.raises(ValueError, match="twice"):
        process_purchase_return(
            shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}, {"purchase_line_id": m_line, "qty": 1}],
            user_id=shop.owner, reason="r",
        )
    with pytest.raises(ValueError, match="Settlement"):
        process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.owner, reason="r", settlement="x")


def test_cashier_cannot_return_to_supplier_but_stock_staff_can(shop):
    pid, (m_line, _) = _purchase(shop)
    with pytest.raises(ValueError, match="not allowed"):
        process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.cashier, reason="r", on_date=DAY)
    process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.stock, reason="r", on_date=DAY)
    assert get_stock(shop.conn, shop.m) == 9


def test_idempotency_key_prevents_double_click(shop):
    pid, (m_line, _) = _purchase(shop)
    args = (shop.conn, pid, [{"purchase_line_id": m_line, "qty": 2}])
    a = process_purchase_return(*args, user_id=shop.owner, reason="r", idempotency_key="k1", on_date=DAY)
    b = process_purchase_return(*args, user_id=shop.owner, reason="r", idempotency_key="k1", on_date=DAY)
    assert a == b and get_stock(shop.conn, shop.m) == 8 and _count(shop.conn, "purchase_returns") == 1


def test_bad_refund_mode_refused_and_nothing_saved(shop):
    pid, (m_line, _) = _purchase(shop, paid=1050000)
    with pytest.raises(ValueError, match="payment method"):
        process_purchase_return(
            shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.owner, reason="r",
            refund_mode="gold coins", on_date=DAY,
        )
    assert get_stock(shop.conn, shop.m) == 10 and _count(shop.conn, "purchase_returns") == 0


def test_debit_notes_are_immutable_and_numbers_sequential(shop):
    pid, (m_line, _) = _purchase(shop)
    a = process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.owner, reason="r", on_date=DAY)
    b = process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 1}], user_id=shop.owner, reason="r", on_date=DAY)
    assert [r["number"] for r in list_purchase_returns(shop.conn, pid)] == [1, 2] and (a, b) == (1, 2)
    with pytest.raises(Exception, match="immutable"):
        shop.conn.execute("UPDATE purchase_returns SET total_paise = 0")
    with pytest.raises(Exception, match="cannot be deleted"):
        shop.conn.execute("DELETE FROM purchase_returns")
    with pytest.raises(Exception, match="immutable"):
        shop.conn.execute("UPDATE purchase_return_lines SET qty = 9")
    assert verify_chain(shop.conn) == (True, None)


def test_original_purchase_is_untouched(shop):
    pid, (m_line, _) = _purchase(shop)
    before = dict(get_purchase(shop.conn, pid)[0])
    process_purchase_return(shop.conn, pid, [{"purchase_line_id": m_line, "qty": 5}], user_id=shop.owner, reason="r", on_date=DAY)
    assert dict(get_purchase(shop.conn, pid)[0]) == before
