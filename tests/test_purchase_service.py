"""Purchases and purchase returns as the screens use them: permissions, one transaction, audit log."""

import sqlite3
from types import SimpleNamespace

import pytest

import core.purchases as purchases_module
from core import auth, purchase_service as svc, supplier_ledger
from core.audit import verify_chain
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.stock import InsufficientStockError, add_movement, get_stock
from db.connection import open_db, transaction


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    ca = auth.create_user(conn, owner, "ca", "ca-password", "accountant")
    supplier = purchases_module.create_supplier(conn, "Ravi Textiles", phone="9999999999")
    pid = create_product(conn, "Shirt", default_cost_paise=50000, gst_rate_percent=5)
    ids = generate_variants(conn, pid, ["XL", "S", "M"], ["Red", "Blue"])
    variants = {}
    for vid in ids:
        row = conn.execute("SELECT size, colour FROM variants WHERE id = ?", (vid,)).fetchone()
        variants[(row["size"], row["colour"])] = vid
    base = conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]  # user set-up entries
    yield SimpleNamespace(conn=conn, owner=owner, store=store, till=till, ca=ca, supplier=supplier,
                          pid=pid, v=variants, base=base)
    conn.close()


def _lines(s, *pairs, qty=10, cost=50000, rate=5):
    return [{"variant_id": s.v[p], "qty": qty, "unit_cost_paise": cost, "tax_rate_percent": rate} for p in pairs]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _actions(shop):
    """Audit actions written since the shop was set up."""
    rows = shop.conn.execute("SELECT action FROM audit_log WHERE id > ? ORDER BY id", (shop.base,))
    return [r["action"] for r in rows]


def _save(s, user=None, **kw):
    args = dict(invoice_no="INV-1", invoice_date="2026-11-01", lines=_lines(s, ("M", "Blue"), ("S", "Red")))
    args.update(kw)
    return svc.save_draft(s.conn, user or s.store, s.supplier, args.pop("invoice_no"), args.pop("invoice_date"),
                          args.pop("lines"), **args)


# ---------------------------------------------------------------- reading what was typed

def test_dates_are_read_as_dd_mm_yyyy_and_iso():
    assert svc.parse_ui_date("04/11/2026") == "2026-11-04"
    assert svc.parse_ui_date(" 4-1-2026 ") == "2026-01-04"
    assert svc.parse_ui_date("2026-11-04") == "2026-11-04"
    assert svc.format_ui_date("2026-11-04") == "04/11/2026"
    for bad in ("31/02/2026", "", "tomorrow", "2026/11/04"):
        with pytest.raises(ValueError, match="valid date"):
            svc.parse_ui_date(bad, "Invoice date")


def test_quantities_are_whole_numbers_only():
    assert svc.parse_qty("12") == 12 and svc.parse_qty("") == 0 and svc.parse_qty(None) == 0
    for bad in ("1.5", "-3", "abc", "1e3"):
        with pytest.raises(ValueError, match="whole number"):
            svc.parse_qty(bad)
    with pytest.raises(ValueError, match="too large"):
        svc.parse_qty("100001")


def test_gst_rate_is_read_without_assuming_any_rate():
    assert svc.parse_tax_rate("") == 0.0 and svc.parse_tax_rate("5") == 5.0 and svc.parse_tax_rate("12.5 %") == 12.5
    for bad in ("abc", "-1", "101", "5.123", "nan"):
        with pytest.raises(ValueError):
            svc.parse_tax_rate(bad)


# ---------------------------------------------------------------- products and the grid

def test_product_search_hides_cost_from_people_who_may_not_see_costs(shop):
    owner_view = svc.find_products(shop.conn, shop.owner, "shirt")
    store_view = svc.find_products(shop.conn, shop.store, "shirt")
    assert owner_view[0]["default_cost_paise"] == 50000
    assert store_view[0]["default_cost_paise"] is None and store_view[0]["name"] == "Shirt"
    with pytest.raises(PermissionDenied):
        svc.find_products(shop.conn, shop.till, "shirt")


def test_matrix_has_sizes_in_shop_order_and_a_cell_for_every_variant(shop):
    with transaction(shop.conn):
        add_movement(shop.conn, shop.v[("M", "Blue")], 7, "opening stock")
    m = svc.product_matrix(shop.conn, shop.store, shop.pid)
    assert m["sizes"] == ["S", "M", "XL"] and m["colours"] == ["Blue", "Red"]
    assert len(m["cells"]) == 6
    cell = next(c for c in m["cells"] if (c["size"], c["colour"]) == ("M", "Blue"))
    assert cell["variant_id"] == shop.v[("M", "Blue")] and cell["stock"] == 7 and cell["barcode"]
    assert m["product"]["default_cost_paise"] is None  # the stock user may not see costs


def test_matrix_errors_are_short(shop):
    with pytest.raises(ValueError, match="Product not found"):
        svc.product_matrix(shop.conn, shop.owner, 9999)
    empty = create_product(shop.conn, "Plain")
    with pytest.raises(ValueError, match="no sizes or colours"):
        svc.product_matrix(shop.conn, shop.owner, empty)


def test_lines_from_matrix_skips_zero_boxes_and_checks_the_rest(shop):
    m = svc.product_matrix(shop.conn, shop.owner, shop.pid)
    lines = svc.lines_from_matrix(
        m, {shop.v[("XL", "Red")]: 2, shop.v[("S", "Blue")]: 5, shop.v[("M", "Red")]: 0},
        unit_cost_paise=50000, tax_rate_percent=5.0,
    )
    assert [(l["size"], l["colour"], l["qty"]) for l in lines] == [("S", "Blue", 5), ("XL", "Red", 2)]
    with pytest.raises(ValueError, match="at least one box"):
        svc.lines_from_matrix(m, {shop.v[("S", "Blue")]: 0}, unit_cost_paise=1, tax_rate_percent=0)
    with pytest.raises(ValueError, match="does not belong"):
        svc.lines_from_matrix(m, {99999: 1}, unit_cost_paise=1, tax_rate_percent=0)
    with pytest.raises(ValueError, match="cost per piece"):
        svc.lines_from_matrix(m, {shop.v[("S", "Blue")]: 1}, unit_cost_paise=None, tax_rate_percent=0)


# ---------------------------------------------------------------- review

def test_review_matches_what_will_be_saved_and_saves_nothing(shop):
    lines = _lines(shop, ("M", "Blue"), ("S", "Red"), qty=3, cost=12345, rate=5)
    r = svc.review_purchase(shop.conn, shop.store, lines, discount_paise=1000, freight_paise=500, paid_paise=2000)
    pid = svc.save_draft(shop.conn, shop.store, shop.supplier, "R-1", "2026-11-01", lines, discount_paise=1000,
                         freight_paise=500, paid_paise=2000)
    row = shop.conn.execute("SELECT * FROM purchases WHERE id = ?", (pid,)).fetchone()
    assert r["total_paise"] == row["total_paise"] and r["tax_paise"] == row["tax_paise"]
    assert r["due_paise"] == row["due_paise"] == row["total_paise"] - 2000
    assert r["total_qty"] == 6 and r["lines"][0]["product"] == "Shirt" and r["lines"][0]["size"] == "M"
    assert _count(shop.conn, "purchases") == 1  # only the real save wrote a row


def test_review_rejects_bad_amounts(shop):
    lines = _lines(shop, ("M", "Blue"), qty=1, cost=10000, rate=0)
    with pytest.raises(ValueError, match="Amount paid"):
        svc.review_purchase(shop.conn, shop.store, lines, paid_paise=10001)
    with pytest.raises(ValueError, match="at least one line"):
        svc.review_purchase(shop.conn, shop.store, [])
    with pytest.raises(ValueError, match="not found"):
        svc.review_purchase(shop.conn, shop.store, [{"variant_id": 99999, "qty": 1, "unit_cost_paise": 1}])


# ---------------------------------------------------------------- draft

def test_draft_does_not_change_stock_and_is_logged(shop):
    pid = _save(shop)
    assert [get_stock(shop.conn, v) for v in shop.v.values()] == [0] * 6
    row = shop.conn.execute("SELECT status, created_by FROM purchases WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "draft" and row["created_by"] == shop.store
    assert _actions(shop) == ["purchase_draft_saved"]


def test_people_without_permission_cannot_save_or_confirm(shop):
    pid = _save(shop)
    for who in (shop.till, shop.ca):
        with pytest.raises(PermissionDenied):
            _save(shop, user=who, invoice_no="X")
        with pytest.raises(PermissionDenied):
            svc.confirm_purchase(shop.conn, who, pid)
        with pytest.raises(PermissionDenied):
            svc.cancel_draft(shop.conn, who, pid)
    assert _count(shop.conn, "purchases") == 1 and get_stock(shop.conn, shop.v[("M", "Blue")]) == 0


def test_draft_needs_an_enabled_supplier_and_an_invoice_number(shop):
    with pytest.raises(ValueError, match="invoice number is required"):
        _save(shop, invoice_no="  ")
    with pytest.raises(ValueError, match="too long"):
        _save(shop, invoice_no="X" * 41)
    with pytest.raises(ValueError, match="choose a supplier"):
        svc.save_draft(shop.conn, shop.store, 9999, "A", "2026-11-01", _lines(shop, ("M", "Blue")))
    supplier_ledger.set_supplier_active(shop.conn, shop.owner, shop.supplier, False)
    with pytest.raises(ValueError, match="disabled"):
        _save(shop)
    assert _count(shop.conn, "purchases") == 0


def test_editing_a_draft_replaces_it_in_one_step(shop):
    old = _save(shop)
    new = _save(shop, invoice_no="INV-1B", replaces_draft_id=old)
    statuses = {r["id"]: r["status"] for r in shop.conn.execute("SELECT id, status FROM purchases")}
    assert statuses == {old: "cancelled", new: "draft"}
    assert "purchase_draft_replaced" in _actions(shop)


def test_a_bad_edit_leaves_the_old_draft_untouched(shop):
    old = _save(shop)
    with pytest.raises(ValueError):
        _save(shop, invoice_no="INV-2", replaces_draft_id=old, lines=[])
    assert shop.conn.execute("SELECT status FROM purchases WHERE id = ?", (old,)).fetchone()[0] == "draft"
    assert _count(shop.conn, "purchases") == 1


def test_a_confirmed_purchase_cannot_be_replaced_as_a_draft(shop):
    done = svc.receive_purchase(shop.conn, shop.store, shop.supplier, "I-9", "2026-11-01", _lines(shop, ("M", "Blue")))
    with pytest.raises(purchases_module.PurchaseStateError):
        _save(shop, invoice_no="I-10", replaces_draft_id=done["purchase_id"])
    assert get_stock(shop.conn, shop.v[("M", "Blue")]) == 10 and _count(shop.conn, "purchases") == 1


# ---------------------------------------------------------------- confirm

def test_confirm_adds_stock_once_and_gives_labels(shop):
    pid = _save(shop, paid_paise=5000)
    done = svc.confirm_purchase(shop.conn, shop.store, pid, payment_mode="upi")
    assert done["voucher_no"].startswith("PUR/26-27/") and done["purchase_id"] == pid
    assert get_stock(shop.conn, shop.v[("M", "Blue")]) == 10 and get_stock(shop.conn, shop.v[("S", "Red")]) == 10
    assert get_stock(shop.conn, shop.v[("XL", "Red")]) == 0
    assert sorted((i["variant_id"], i["copies"]) for i in done["label_items"]) == sorted(
        [(shop.v[("M", "Blue")], 10), (shop.v[("S", "Red")], 10)])
    pay = shop.conn.execute("SELECT mode, amount_paise FROM payments WHERE document_type = 'purchase'").fetchone()
    assert (pay["mode"], pay["amount_paise"]) == ("upi", 5000)
    assert _actions(shop) == ["purchase_draft_saved", "purchase_confirmed"]


def test_double_confirm_is_refused_and_stock_is_added_once(shop):
    pid = _save(shop)
    svc.confirm_purchase(shop.conn, shop.store, pid)
    with pytest.raises(purchases_module.PurchaseStateError):
        svc.confirm_purchase(shop.conn, shop.store, pid)
    assert get_stock(shop.conn, shop.v[("M", "Blue")]) == 10
    assert _actions(shop).count("purchase_confirmed") == 1


def test_confirm_needs_a_valid_payment_method_and_saves_nothing_otherwise(shop):
    pid = _save(shop)
    with pytest.raises(ValueError, match="payment method"):
        svc.confirm_purchase(shop.conn, shop.store, pid, payment_mode="gold")
    assert shop.conn.execute("SELECT status FROM purchases WHERE id = ?", (pid,)).fetchone()[0] == "draft"
    assert _count(shop.conn, "number_series") == 0 and get_stock(shop.conn, shop.v[("M", "Blue")]) == 0


def test_receive_is_one_transaction_a_failure_leaves_no_draft_no_stock_no_number(shop, monkeypatch):
    real = purchases_module.add_movement
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")
        return real(*a, **k)

    monkeypatch.setattr(purchases_module, "add_movement", flaky)
    with pytest.raises(RuntimeError):
        svc.receive_purchase(shop.conn, shop.store, shop.supplier, "I-1", "2026-11-01",
                             _lines(shop, ("M", "Blue"), ("S", "Red")), paid_paise=1000)
    assert _count(shop.conn, "purchases") == 0 and _count(shop.conn, "purchase_lines") == 0
    assert _count(shop.conn, "payments") == 0 and _count(shop.conn, "number_series") == 0
    assert _actions(shop) == [] and get_stock(shop.conn, shop.v[("M", "Blue")]) == 0
    monkeypatch.setattr(purchases_module, "add_movement", real)
    done = svc.receive_purchase(shop.conn, shop.store, shop.supplier, "I-1", "2026-11-01", _lines(shop, ("M", "Blue")))
    assert done["voucher_no"].endswith("000001")  # the failed try released its number


def test_receive_saves_and_confirms_together(shop):
    done = svc.receive_purchase(shop.conn, shop.store, shop.supplier, "I-2", "2026-11-01",
                                _lines(shop, ("M", "Blue"), ("S", "Red")), received_date="2026-11-02")
    row = shop.conn.execute("SELECT status, received_date FROM purchases WHERE id = ?", (done["purchase_id"],)).fetchone()
    assert (row["status"], row["received_date"]) == ("confirmed", "2026-11-02")
    assert get_stock(shop.conn, shop.v[("M", "Blue")]) == 10
    assert _actions(shop) == ["purchase_draft_saved", "purchase_confirmed"]


def test_receive_by_a_user_without_permission_saves_nothing(shop):
    with pytest.raises(PermissionDenied):
        svc.receive_purchase(shop.conn, shop.till, shop.supplier, "I-3", "2026-11-01", _lines(shop, ("M", "Blue")))
    assert _count(shop.conn, "purchases") == 0 and _actions(shop) == []


# ---------------------------------------------------------------- cancel

def test_cancel_draft_keeps_the_record_and_confirmed_cannot_be_cancelled(shop):
    pid = _save(shop)
    svc.cancel_draft(shop.conn, shop.store, pid, "typed wrong supplier")
    assert shop.conn.execute("SELECT status FROM purchases WHERE id = ?", (pid,)).fetchone()[0] == "cancelled"
    assert "purchase_draft_cancelled" in _actions(shop)
    with pytest.raises(purchases_module.PurchaseStateError):
        svc.confirm_purchase(shop.conn, shop.store, pid)
    done = svc.receive_purchase(shop.conn, shop.store, shop.supplier, "I-4", "2026-11-01", _lines(shop, ("M", "Blue")))
    with pytest.raises(purchases_module.PurchaseStateError):
        svc.cancel_draft(shop.conn, shop.store, done["purchase_id"])
    assert get_stock(shop.conn, shop.v[("M", "Blue")]) == 10


# ---------------------------------------------------------------- duplicate invoice warning

def test_duplicate_invoice_is_a_warning_with_details(shop):
    done = svc.receive_purchase(shop.conn, shop.store, shop.supplier, "Inv/77", "2026-11-01", _lines(shop, ("M", "Blue")))
    found = svc.duplicate_invoices(shop.conn, shop.store, shop.supplier, " inv/77 ", "2026-11-20")
    assert [(f["purchase_id"], f["status"], f["voucher_no"]) for f in found] == [
        (done["purchase_id"], "confirmed", done["voucher_no"])]
    assert svc.duplicate_invoices(shop.conn, shop.store, shop.supplier, "Inv/77", "2027-04-02") == []  # next fin. year
    assert svc.duplicate_invoices(shop.conn, shop.store, shop.supplier, "", "2026-11-20") == []
    again = _save(shop, invoice_no="Inv/77")  # still allowed: it is only a warning
    assert again != done["purchase_id"]
    svc.cancel_draft(shop.conn, shop.store, again)
    assert len(svc.duplicate_invoices(shop.conn, shop.store, shop.supplier, "Inv/77", "2026-11-20")) == 1


# ---------------------------------------------------------------- reading purchases

def test_list_and_detail(shop):
    draft = _save(shop, invoice_no="D-1")
    done = svc.receive_purchase(shop.conn, shop.store, shop.supplier, "C-1", "2026-11-01",
                                _lines(shop, ("M", "Blue")), paid_paise=1000)
    rows = svc.list_purchases(shop.conn, shop.store)
    assert [r["id"] for r in rows] == [done["purchase_id"], draft]
    assert [r["id"] for r in svc.list_purchases(shop.conn, shop.store, status="draft")] == [draft]
    assert [r["id"] for r in svc.list_purchases(shop.conn, shop.store, text="c-1")] == [done["purchase_id"]]
    assert [r["id"] for r in svc.list_purchases(shop.conn, shop.store, supplier_id=shop.supplier + 1)] == []
    confirmed = rows[0]
    assert confirmed["paid_paise"] == 1000 and confirmed["outstanding_paise"] == confirmed["total_paise"] - 1000
    d = svc.purchase_detail(shop.conn, shop.store, done["purchase_id"])
    assert d["supplier_name"] == "Ravi Textiles" and d["lines"][0]["product"] == "Shirt" and d["lines"][0]["size"] == "M"
    with pytest.raises(ValueError, match="Unknown purchase status"):
        svc.list_purchases(shop.conn, shop.store, status="weird")
    with pytest.raises(PermissionDenied):
        svc.list_purchases(shop.conn, shop.till)


# ---------------------------------------------------------------- paying a supplier

def _confirmed(shop, total_lines=None, invoice="P-1"):
    return svc.receive_purchase(shop.conn, shop.store, shop.supplier, invoice, "2026-11-01",
                                total_lines or _lines(shop, ("M", "Blue"), qty=10, cost=10000, rate=0))["purchase_id"]


def test_pay_a_bill_in_parts_and_the_balance_follows(shop):
    pid = _confirmed(shop)  # Rs 1,000.00
    svc.pay_supplier_bill(shop.conn, shop.owner, pid, 40000, "cash", reference="slip 1")
    svc.pay_supplier_bill(shop.conn, shop.owner, pid, 60000, "upi")
    bill = supplier_ledger.supplier_outstanding_bills(shop.conn, shop.supplier, user_id=shop.owner)
    assert bill == []
    assert supplier_ledger.supplier_balance(shop.conn, shop.supplier, user_id=shop.owner)["balance_paise"] == 0
    assert "supplier_payment" in _actions(shop)


def test_overpaying_a_bill_is_refused_with_rupees_in_the_message(shop):
    pid = _confirmed(shop)
    with pytest.raises(ValueError, match=r"only has ₹1,000\.00 left"):
        svc.pay_supplier_bill(shop.conn, shop.owner, pid, 100001, "cash")
    assert _count(shop.conn, "supplier_payments") == 0


def test_paying_needs_cash_permission_and_a_confirmed_bill(shop):
    pid = _confirmed(shop)
    with pytest.raises(PermissionDenied, match="pay suppliers"):
        svc.pay_supplier_bill(shop.conn, shop.store, pid, 100, "cash")  # the storekeeper has no cash permission
    draft = _save(shop, invoice_no="D-2")
    with pytest.raises(ValueError, match="confirmed"):
        svc.pay_supplier_bill(shop.conn, shop.owner, draft, 100, "cash")
    with pytest.raises(ValueError, match="not found"):
        svc.pay_supplier_bill(shop.conn, shop.owner, 9999, 100, "cash")
    assert _count(shop.conn, "supplier_payments") == 0


def test_the_same_payment_key_pays_only_once(shop):
    pid = _confirmed(shop)
    a = svc.pay_supplier_bill(shop.conn, shop.owner, pid, 25000, "cash", idempotency_key="k-1")
    b = svc.pay_supplier_bill(shop.conn, shop.owner, pid, 25000, "cash", idempotency_key="k-1")
    assert a == b and _count(shop.conn, "supplier_payments") == 1


# ---------------------------------------------------------------- purchase returns

def test_only_purchases_with_something_left_to_return_are_offered(shop):
    p1 = _confirmed(shop, invoice="A-1")
    _save(shop, invoice_no="D-3")  # a draft: never offered
    found = svc.find_returnable_purchases(shop.conn, shop.store)
    assert [(f["purchase_id"], f["returnable"]) for f in found] == [(p1, 10)]
    assert svc.find_returnable_purchases(shop.conn, shop.store, text="zzz") == []
    line = svc.returnable_lines(shop.conn, shop.store, p1)[0]
    svc.return_to_supplier(shop.conn, shop.store, p1, {line["purchase_line_id"]: 10}, "all faulty")
    assert svc.find_returnable_purchases(shop.conn, shop.store) == []
    with pytest.raises(PermissionDenied):
        svc.find_returnable_purchases(shop.conn, shop.till)


def test_returnable_lines_never_offer_more_than_is_in_stock(shop):
    p = _confirmed(shop)
    with transaction(shop.conn):
        add_movement(shop.conn, shop.v[("M", "Blue")], -7, "sale")
    line = svc.returnable_lines(shop.conn, shop.store, p)[0]
    assert (line["qty_bought"], line["qty_returnable"], line["sellable_now"], line["max_qty"]) == (10, 10, 3, 3)
    with pytest.raises(ValueError, match="confirmed"):
        svc.returnable_lines(shop.conn, shop.store, _save(shop, invoice_no="D-4"))


def test_return_to_supplier_reduces_stock_makes_a_debit_note_and_is_logged(shop):
    p = _confirmed(shop, invoice="R-1")  # Rs 1,000.00, nothing paid
    line = svc.returnable_lines(shop.conn, shop.store, p)[0]
    result = svc.return_to_supplier(shop.conn, shop.store, p, {line["purchase_line_id"]: 4, 99999: 0}, "torn pieces")
    assert result["debit_note_no"].startswith("DN/26-27/") and result["total_paise"] == 40000
    assert result["due_reduced_paise"] == 40000 and result["refund_paise"] == 0
    assert get_stock(shop.conn, shop.v[("M", "Blue")]) == 6
    assert supplier_ledger.supplier_balance(shop.conn, shop.supplier, user_id=shop.owner)["balance_paise"] == 60000
    assert "purchase_return_created" in _actions(shop)
    hist = svc.purchase_return_history(shop.conn, shop.store, p)
    assert [(h["debit_note_no"], h["total_paise"]) for h in hist] == [(result["debit_note_no"], 40000)]


def test_refund_when_the_bill_is_already_paid(shop):
    p = _confirmed(shop, invoice="R-2")
    svc.pay_supplier_bill(shop.conn, shop.owner, p, 100000, "upi")
    line = svc.returnable_lines(shop.conn, shop.store, p)[0]
    r = svc.return_to_supplier(shop.conn, shop.store, p, {line["purchase_line_id"]: 2}, "wrong colour",
                               settlement="refund", refund_mode="upi")
    assert (r["refund_paise"], r["supplier_credit_paise"], r["due_reduced_paise"]) == (20000, 0, 0)
    r2 = svc.return_to_supplier(shop.conn, shop.store, p, {line["purchase_line_id"]: 1}, "wrong colour",
                                settlement="credit")
    assert (r2["refund_paise"], r2["supplier_credit_paise"]) == (0, 10000)


def test_returning_too_much_or_without_a_reason_saves_nothing(shop):
    p = _confirmed(shop, invoice="R-3")
    lid = svc.returnable_lines(shop.conn, shop.store, p)[0]["purchase_line_id"]
    with pytest.raises(ValueError, match="only 10 can still be returned"):
        svc.return_to_supplier(shop.conn, shop.store, p, {lid: 11}, "x")
    with pytest.raises(ValueError, match="reason"):
        svc.return_to_supplier(shop.conn, shop.store, p, {lid: 1}, "  ")
    with pytest.raises(ValueError, match="how many pieces"):
        svc.return_to_supplier(shop.conn, shop.store, p, {lid: 0}, "x")
    with transaction(shop.conn):
        add_movement(shop.conn, shop.v[("M", "Blue")], -9, "sale")
    with pytest.raises(InsufficientStockError, match="only 1 sellable"):
        svc.return_to_supplier(shop.conn, shop.store, p, {lid: 2}, "x")
    assert _count(shop.conn, "purchase_returns") == 0 and get_stock(shop.conn, shop.v[("M", "Blue")]) == 1


def test_the_same_return_key_returns_only_once(shop):
    p = _confirmed(shop, invoice="R-4")
    lid = svc.returnable_lines(shop.conn, shop.store, p)[0]["purchase_line_id"]
    a = svc.return_to_supplier(shop.conn, shop.store, p, {lid: 3}, "faulty", idempotency_key="rk-1")
    b = svc.return_to_supplier(shop.conn, shop.store, p, {lid: 3}, "faulty", idempotency_key="rk-1")
    assert a["return_id"] == b["return_id"] and get_stock(shop.conn, shop.v[("M", "Blue")]) == 7


def test_a_cashier_cannot_return_goods(shop):
    p = _confirmed(shop, invoice="R-5")
    lid = svc.returnable_lines(shop.conn, shop.store, p)[0]["purchase_line_id"]
    with pytest.raises(PermissionDenied):
        svc.return_to_supplier(shop.conn, shop.till, p, {lid: 1}, "x")
    with pytest.raises(PermissionDenied):
        svc.returnable_lines(shop.conn, shop.till, p)
    assert _count(shop.conn, "purchase_returns") == 0


# ---------------------------------------------------------------- messages and the log

def test_expected_errors_show_their_text_and_faults_stay_hidden():
    text, expected = svc.safe_message(PermissionDenied("You are not allowed to pay suppliers."))
    assert expected and text == "You are not allowed to pay suppliers."
    assert svc.safe_message(InsufficientStockError("only 1 sellable"))[1] is True
    text, expected = svc.safe_message(sqlite3.OperationalError("no such table: secret_table /home/anu/shop.db"))
    assert not expected and "secret" not in text and "shop.db" not in text and text == svc.GENERIC_ERROR


def test_the_audit_log_stays_valid_after_everything(shop):
    p = _confirmed(shop, invoice="Z-1")
    _save(shop, invoice_no="Z-2")
    svc.pay_supplier_bill(shop.conn, shop.owner, p, 5000, "cash")
    lid = svc.returnable_lines(shop.conn, shop.store, p)[0]["purchase_line_id"]
    svc.return_to_supplier(shop.conn, shop.store, p, {lid: 1}, "faulty")
    assert verify_chain(shop.conn) == (True, None)