from types import SimpleNamespace

import pytest

from core.audit import verify_chain
from core.products import create_product, generate_variants
from core.stock import InsufficientStockError, add_movement, get_stock
from core.stock_counts import (
    apply_count, cancel_count, get_count_sheet, record_count, start_count, variance_report,
)
from core.stock_ops import list_adjustments, mark_damaged, mark_missing, review_returned
from db.connection import open_db, transaction


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('store', 'x', 'stock')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('till', 'x', 'cashier')")
    shirt = create_product(conn, "Shirt", category="Shirts")
    jeans = create_product(conn, "Jeans", category="Jeans")
    m_blue, l_blue = generate_variants(conn, shirt, ["M", "L"], ["Blue"])
    (j32,) = generate_variants(conn, jeans, ["32"], ["Black"])
    with transaction(conn):
        for v in (m_blue, l_blue, j32):
            add_movement(conn, v, 10, "opening stock")
    yield SimpleNamespace(conn=conn, owner=1, stock=2, cashier=3, m_blue=m_blue, l_blue=l_blue, j32=j32)
    conn.close()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------- damaged / missing ----------

def test_mark_damaged_moves_stock_out_of_sellable(shop):
    adj = mark_damaged(shop.conn, shop.m_blue, 3, "torn at collar", user_id=shop.stock)
    assert get_stock(shop.conn, shop.m_blue) == 7
    assert get_stock(shop.conn, shop.m_blue, "damaged") == 3
    row = list_adjustments(shop.conn, shop.m_blue)[0]
    assert (row["id"], row["kind"], row["qty"]) == (adj, "damaged", 3)


def test_mark_missing_reduces_sellable_and_is_recorded(shop):
    mark_missing(shop.conn, shop.m_blue, 2, "not on shelf", user_id=shop.owner)
    assert get_stock(shop.conn, shop.m_blue) == 8
    assert list_adjustments(shop.conn, kind="missing")[0]["reason"] == "not on shelf"
    last = shop.conn.execute("SELECT * FROM stock_movements ORDER BY id DESC LIMIT 1").fetchone()
    assert last["doc_type"] == "stock_adjustment" and last["qty_change"] == -2 and last["user_id"] == shop.owner


def test_cannot_damage_more_than_sellable(shop):
    with pytest.raises(InsufficientStockError):
        mark_damaged(shop.conn, shop.m_blue, 11, "oops", user_id=shop.owner)
    assert get_stock(shop.conn, shop.m_blue) == 10
    assert get_stock(shop.conn, shop.m_blue, "damaged") == 0
    assert _count(shop.conn, "stock_adjustments") == 0  # nothing half-saved


@pytest.mark.parametrize("qty,reason", [(0, "x"), (-1, "x"), (1.5, "x"), (1, ""), (1, "   ")])
def test_damage_needs_valid_qty_and_reason(shop, qty, reason):
    with pytest.raises(ValueError):
        mark_damaged(shop.conn, shop.m_blue, qty, reason, user_id=shop.owner)
    assert get_stock(shop.conn, shop.m_blue) == 10


def test_cashier_cannot_adjust_stock(shop):
    with pytest.raises(ValueError, match="not allowed"):
        mark_damaged(shop.conn, shop.m_blue, 1, "x", user_id=shop.cashier)
    with pytest.raises(ValueError, match="not allowed"):
        mark_missing(shop.conn, shop.m_blue, 1, "x", user_id=shop.cashier)
    assert get_stock(shop.conn, shop.m_blue) == 10


def test_unknown_variant_or_user_refused(shop):
    with pytest.raises(ValueError, match="variant"):
        mark_damaged(shop.conn, 9999, 1, "x", user_id=shop.owner)
    with pytest.raises(ValueError, match="User"):
        mark_damaged(shop.conn, shop.m_blue, 1, "x", user_id=9999)


def test_review_returned_to_sellable_or_damaged(shop):
    with transaction(shop.conn):
        add_movement(shop.conn, shop.m_blue, 3, "sale return", bucket="returned_pending")
    review_returned(shop.conn, shop.m_blue, 2, resellable=True, user_id=shop.stock)
    review_returned(shop.conn, shop.m_blue, 1, resellable=False, user_id=shop.owner, reason="stained")
    assert get_stock(shop.conn, shop.m_blue, "returned_pending") == 0
    assert get_stock(shop.conn, shop.m_blue) == 12
    assert get_stock(shop.conn, shop.m_blue, "damaged") == 1
    with pytest.raises(InsufficientStockError):
        review_returned(shop.conn, shop.m_blue, 1, resellable=True, user_id=shop.owner)


def test_adjustments_are_append_only(shop):
    mark_damaged(shop.conn, shop.m_blue, 1, "x", user_id=shop.owner)
    with pytest.raises(Exception, match="append-only"):
        shop.conn.execute("UPDATE stock_adjustments SET qty = 5")
    with pytest.raises(Exception, match="append-only"):
        shop.conn.execute("DELETE FROM stock_adjustments")


def test_adjustment_audit_chain_stays_valid(shop):
    mark_damaged(shop.conn, shop.m_blue, 1, "x", user_id=shop.owner)
    mark_missing(shop.conn, shop.l_blue, 1, "y", user_id=shop.owner)
    assert verify_chain(shop.conn) == (True, None)
    assert _count(shop.conn, "audit_log") == 2


# ---------- stock count ----------

def test_count_freezes_expected_and_applies_difference(shop):
    cid = start_count(shop.conn, user_id=shop.stock, variant_ids=[shop.m_blue, shop.l_blue])
    record_count(shop.conn, cid, shop.m_blue, 8, user_id=shop.stock, reason="2 not found")
    record_count(shop.conn, cid, shop.l_blue, 10, user_id=shop.stock)
    assert get_stock(shop.conn, shop.m_blue) == 10  # nothing changes before approval
    rep = variance_report(shop.conn, cid)
    assert [(r["variant_id"], r["expected_qty"], r["counted_qty"], r["difference"]) for r in rep] == [
        (shop.m_blue, 10, 8, -2)
    ]
    summary = apply_count(shop.conn, cid, approver_id=shop.owner)
    assert summary == {"variants_adjusted": 1, "units_added": 0, "units_removed": 2}
    assert get_stock(shop.conn, shop.m_blue) == 8
    assert get_stock(shop.conn, shop.l_blue) == 10
    mv = shop.conn.execute("SELECT * FROM stock_movements WHERE doc_type = 'stock_count'").fetchall()
    assert len(mv) == 1 and mv[0]["doc_id"] == cid and mv[0]["user_id"] == shop.owner


def test_count_can_add_stock_found(shop):
    cid = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.j32])
    record_count(shop.conn, cid, shop.j32, 12, user_id=shop.owner, reason="found in godown")
    assert apply_count(shop.conn, cid, approver_id=shop.owner)["units_added"] == 2
    assert get_stock(shop.conn, shop.j32) == 12


def test_apply_needs_owner_approval(shop):
    cid = start_count(shop.conn, user_id=shop.stock, variant_ids=[shop.m_blue])
    record_count(shop.conn, cid, shop.m_blue, 9, user_id=shop.stock, reason="x")
    with pytest.raises(ValueError, match="not allowed"):
        apply_count(shop.conn, cid, approver_id=shop.stock)
    with pytest.raises(ValueError, match="not allowed"):
        apply_count(shop.conn, cid, approver_id=shop.cashier)
    assert get_stock(shop.conn, shop.m_blue) == 10


def test_difference_without_reason_blocks_apply(shop):
    cid = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    record_count(shop.conn, cid, shop.m_blue, 9, user_id=shop.owner)
    with pytest.raises(ValueError, match="reason"):
        apply_count(shop.conn, cid, approver_id=shop.owner)
    assert get_stock(shop.conn, shop.m_blue) == 10
    record_count(shop.conn, cid, shop.m_blue, 9, user_id=shop.owner, reason="miscount fixed")
    apply_count(shop.conn, cid, approver_id=shop.owner)
    assert get_stock(shop.conn, shop.m_blue) == 9


def test_count_cannot_be_applied_twice(shop):
    cid = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    record_count(shop.conn, cid, shop.m_blue, 9, user_id=shop.owner, reason="x")
    apply_count(shop.conn, cid, approver_id=shop.owner)
    with pytest.raises(ValueError, match="already applied"):
        apply_count(shop.conn, cid, approver_id=shop.owner)
    assert get_stock(shop.conn, shop.m_blue) == 9
    with pytest.raises(ValueError, match="already applied"):
        record_count(shop.conn, cid, shop.m_blue, 5, user_id=shop.owner)


def test_applied_count_lines_are_frozen_in_database(shop):
    cid = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    record_count(shop.conn, cid, shop.m_blue, 9, user_id=shop.owner, reason="x")
    apply_count(shop.conn, cid, approver_id=shop.owner)
    with pytest.raises(Exception, match="closed"):
        shop.conn.execute("UPDATE stock_count_lines SET counted_qty = 1")
    with pytest.raises(Exception, match="closed"):
        shop.conn.execute("UPDATE stock_counts SET status = 'open'")
    with pytest.raises(Exception, match="cannot be deleted"):
        shop.conn.execute("DELETE FROM stock_count_lines")


def test_stale_count_is_refused_when_stock_moved(shop):
    cid = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    record_count(shop.conn, cid, shop.m_blue, 9, user_id=shop.owner, reason="x")
    mark_damaged(shop.conn, shop.m_blue, 1, "torn", user_id=shop.owner)  # stock moves during the count
    with pytest.raises(ValueError, match="Stock changed"):
        apply_count(shop.conn, cid, approver_id=shop.owner)
    assert get_stock(shop.conn, shop.m_blue) == 9  # only the damage, no count adjustment


def test_blind_count_hides_expected_until_closed(shop):
    cid = start_count(shop.conn, user_id=shop.stock, variant_ids=[shop.m_blue], blind=True)
    line = get_count_sheet(shop.conn, cid)["lines"][0]
    assert line["expected_qty"] is None and line["difference"] is None
    assert get_count_sheet(shop.conn, cid, reveal=True)["lines"][0]["expected_qty"] == 10
    record_count(shop.conn, cid, shop.m_blue, 10, user_id=shop.stock)
    apply_count(shop.conn, cid, approver_id=shop.owner)
    assert get_count_sheet(shop.conn, cid)["lines"][0]["expected_qty"] == 10


def test_scope_filters_and_empty_scope(shop):
    by_cat = start_count(shop.conn, user_id=shop.owner, category="jeans")
    assert [l["variant_id"] for l in get_count_sheet(shop.conn, by_cat)["lines"]] == [shop.j32]
    full = start_count(shop.conn, user_id=shop.owner)
    assert len(get_count_sheet(shop.conn, full)["lines"]) == 3
    with pytest.raises(ValueError, match="No active variants"):
        start_count(shop.conn, user_id=shop.owner, category="Sarees")
    with pytest.raises(ValueError, match="at least one"):
        start_count(shop.conn, user_id=shop.owner, variant_ids=[])


def test_count_rejects_bad_input(shop):
    cid = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    with pytest.raises(ValueError, match="whole number"):
        record_count(shop.conn, cid, shop.m_blue, -1, user_id=shop.owner)
    with pytest.raises(ValueError, match="not part"):
        record_count(shop.conn, cid, shop.l_blue, 1, user_id=shop.owner)
    with pytest.raises(ValueError, match="Nothing has been counted"):
        apply_count(shop.conn, cid, approver_id=shop.owner)
    with pytest.raises(ValueError, match="not allowed"):
        start_count(shop.conn, user_id=shop.cashier)


def test_cancel_count_leaves_stock_alone(shop):
    cid = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    record_count(shop.conn, cid, shop.m_blue, 0, user_id=shop.owner, reason="x")
    with pytest.raises(ValueError, match="reason"):
        cancel_count(shop.conn, cid, user_id=shop.owner, reason=" ")
    cancel_count(shop.conn, cid, user_id=shop.owner, reason="counted wrong rack")
    assert get_stock(shop.conn, shop.m_blue) == 10
    with pytest.raises(ValueError, match="already cancelled"):
        apply_count(shop.conn, cid, approver_id=shop.owner)


def test_count_numbers_are_sequential_and_audit_valid(shop):
    a = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    b = start_count(shop.conn, user_id=shop.owner, variant_ids=[shop.m_blue])
    nums = [shop.conn.execute("SELECT count_no FROM stock_counts WHERE id = ?", (i,)).fetchone()[0] for i in (a, b)]
    assert nums[0].endswith("000001") and nums[1].endswith("000002") and nums[0].startswith("CNT/")
    assert verify_chain(shop.conn) == (True, None)
