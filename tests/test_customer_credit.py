"""Credit sales: dues from bills/payments/returns, receipts, reversal, credit limit, due dates, ageing."""

import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.customer_credit as cc
from core import auth
from core.audit import verify_chain
from core.auth import AuthError
from core.customer_credit import (
    CreditLimitExceeded, customer_dues, customer_outstanding_paise, dues_report, get_receipt, list_receipts,
    receive_payment, reverse_receipt, set_credit_terms, set_due_date,
)
from core.guarded import return_items, sell
from core.permissions import PermissionDenied, has_permission, set_user_permission
from core.products import create_product, generate_variants
from core.returns import _due_remaining, returnable_lines
from core.sales import get_sale
from core.settings import set_setting
from core.stock import add_movement, get_stock
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction
from db.schema import MIGRATIONS

TODAY = date.today()
FIN_YEAR = f"{TODAY.year % 100:02d}-{(TODAY.year + 1) % 100:02d}" if TODAY.month >= 4 \
    else f"{(TODAY.year - 1) % 100:02d}-{TODAY.year % 100:02d}"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    ca = auth.create_user(conn, owner, "ca", "ca-password", "accountant")
    ensure_default_tax_rules(conn)
    set_setting(conn, "credit_sales_enabled", 1)
    pid = create_product(conn, "Shirt", hsn="6205", default_price_paise=100000)  # Rs 1,000
    (v,) = generate_variants(conn, pid, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, v, 50, "opening stock")
    ids = {}
    for name in ("Ravi", "Meena", "Walkin"):
        ids[name] = conn.execute(
            "INSERT INTO customers(name, phone) VALUES (?, ?)", (name, "98765" + str(10000 + len(ids)))
        ).lastrowid
    set_credit_terms(conn, ids["Ravi"], user_id=owner, credit_enabled=True, credit_limit_paise=500000)   # Rs 5,000
    set_credit_terms(conn, ids["Meena"], user_id=owner, credit_enabled=True, credit_limit_paise=500000)
    yield SimpleNamespace(conn=conn, owner=owner, till=till, store=store, ca=ca, v=v,
                          ravi=ids["Ravi"], meena=ids["Meena"], nocredit=ids["Walkin"])
    conn.close()


def _credit_bill(s, qty=2, cash=0, customer=None, user=None, **kw):
    """qty shirts at Rs 1,000; `cash` paid now; the rest is credit."""
    pay = [{"mode": "cash", "amount_paise": cash}] if cash else []
    return sell(
        s.conn, [{"variant_id": s.v, "qty": qty}], pay, user_id=user or s.till,
        customer_id=customer or s.ravi, **kw,
    )


def _count(s, table):
    return s.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _old_bill(s, customer, total_paise, days_ago):
    """A bill dated `days_ago` days back, entirely on credit (inserted directly: bill dates cannot be edited)."""
    when = (TODAY - timedelta(days=days_ago)).isoformat() + " 10:00:00"
    n = _count(s, "sales") + 1000
    return s.conn.execute(
        "INSERT INTO sales(series, number, invoice_no, fin_year, customer_id, user_id, tax_mode, subtotal_paise, "
        "taxable_paise, total_paise, created_at) VALUES ('INV', ?, ?, '25-26', ?, ?, 'unregistered', ?, ?, ?, ?)",
        (n, f"OLD/{n}", customer, s.owner, total_paise, total_paise, total_paise, when),
    ).lastrowid


# ---------------------------------------------------------------- schema

def test_migration_13_is_registered_and_tables_are_append_only(shop):
    assert 13 in MIGRATIONS
    s = shop
    bill = _credit_bill(s)
    rid = receive_payment(s.conn, amount_paise=50000, mode="cash", user_id=s.till, sale_id=bill)
    for sql in (
        "UPDATE customer_receipts SET amount_paise = 1 WHERE id = ?",
        "DELETE FROM customer_receipts WHERE id = ?",
        "UPDATE customer_receipt_allocations SET amount_paise = 1 WHERE receipt_id = ?",
        "DELETE FROM customer_receipt_allocations WHERE receipt_id = ?",
    ):
        with pytest.raises(Exception, match="cannot be|append-only"):
            s.conn.execute(sql, (rid,))
    with pytest.raises(Exception, match="append-only"):
        s.conn.execute("UPDATE sale_due_dates SET due_date = '2030-01-01' WHERE sale_id = ?", (bill,))
    with pytest.raises(Exception, match="append-only"):
        s.conn.execute("DELETE FROM sale_due_dates WHERE sale_id = ?", (bill,))


def test_old_credit_module_is_gone_and_the_old_ledger_is_never_written(shop):
    root = Path(__file__).resolve().parent.parent
    assert not (root / "core" / "credit_sales.py").exists()
    for path in list((root / "core").rglob("*.py")) + list((root / "ui").rglob("*.py")):
        assert "credit_sales import" not in path.read_text(encoding="utf-8", errors="ignore"), path.name
        assert "import credit_sales" not in path.read_text(encoding="utf-8", errors="ignore"), path.name
    s = shop
    bill = _credit_bill(s)
    receive_payment(s.conn, amount_paise=50000, mode="cash", user_id=s.till, sale_id=bill)
    assert _count(s, "customer_dues_ledger") == 0


# ---------------------------------------------------------------- dues come from bills, payments, returns

def test_credit_bill_makes_a_due_that_matches_the_bill(shop):
    s = shop
    bill = _credit_bill(s, qty=3, cash=100000)                         # 3,000 bill, 1,000 paid
    d = customer_dues(s.conn, s.ravi, user_id=s.till)
    assert d["outstanding_paise"] == 200000 == get_sale(s.conn, bill)["due_paise"]
    assert d["outstanding_paise"] == _due_remaining(s.conn, s.conn.execute("SELECT * FROM sales WHERE id=?", (bill,)).fetchone())
    assert d["available_credit_paise"] == 300000                        # limit 5,000 - 2,000 due
    assert [b["invoice_no"] for b in d["bills"]] == [get_sale(s.conn, bill)["sale"]["invoice_no"]]
    assert all(isinstance(v, int) for v in (d["outstanding_paise"], d["credit_limit_paise"], d["overdue_paise"]))
    assert customer_outstanding_paise(s.conn, s.meena) == 0


def test_fully_paid_bill_owes_nothing_and_records_no_due_date(shop):
    s = shop
    _credit_bill(s, qty=1, cash=100000)
    assert customer_dues(s.conn, s.ravi, user_id=s.till)["outstanding_paise"] == 0
    assert _count(s, "sale_due_dates") == 0


def test_cancelled_bills_owe_nothing(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    s.conn.execute("UPDATE sales SET status = 'cancelled' WHERE id = ?", (bill,))
    assert customer_outstanding_paise(s.conn, s.ravi) == 0


def test_a_return_on_a_credit_bill_reduces_the_due(shop):
    s = shop
    bill = _credit_bill(s, qty=3, cash=100000)                          # due 2,000
    line = returnable_lines(s.conn, bill)[0]["sale_line_id"]
    return_items(s.conn, bill, [{"sale_line_id": line, "qty": 1, "reason": "wrong size"}], user_id=s.till)
    assert customer_dues(s.conn, s.ravi, user_id=s.till)["outstanding_paise"] == 100000
    # and receiving money still works on the reduced due, but not above it
    with pytest.raises(ValueError, match="owes only"):
        receive_payment(s.conn, amount_paise=150000, mode="cash", user_id=s.till, customer_id=s.ravi)
    receive_payment(s.conn, amount_paise=100000, mode="cash", user_id=s.till, customer_id=s.ravi)
    assert customer_outstanding_paise(s.conn, s.ravi) == 0


# ---------------------------------------------------------------- receiving payments

def test_receipt_against_a_bill_is_numbered_and_reduces_the_due(shop):
    s = shop
    bill = _credit_bill(s, qty=3)                                       # 3,000 due
    rid = receive_payment(s.conn, amount_paise=100000, mode="upi", user_id=s.till, sale_id=bill, reference="UTR1")
    r = get_receipt(s.conn, rid, user_id=s.till)
    assert r["receipt_no"] == f"RCT/{FIN_YEAR}/000001"
    assert r["amount_paise"] == 100000 and r["mode"] == "upi" and r["reference"] == "UTR1"
    assert [(a["sale_id"], a["amount_paise"]) for a in r["allocations"]] == [(bill, 100000)]
    assert r["customer_outstanding_paise"] == 200000 and r["customer_name"] == "Ravi"
    sale = get_sale(s.conn, bill)
    assert sale["due_paise"] == 200000
    pay = [p for p in sale["payments"] if p["reference"] == r["receipt_no"]]
    assert len(pay) == 1 and pay[0]["amount_paise"] == 100000 and pay[0]["mode"] == "upi" and pay[0]["user_id"] == s.till
    second = receive_payment(s.conn, amount_paise=1000, mode="cash", user_id=s.till, sale_id=bill)
    assert get_receipt(s.conn, second, user_id=s.till)["receipt_no"].endswith("000002")
    assert verify_chain(s.conn) == (True, None)
    entry = s.conn.execute("SELECT * FROM audit_log WHERE action = 'customer_payment_received' ORDER BY id").fetchone()
    assert entry["entity_id"] == rid and "Ravi" not in (entry["after_json"] or "") and "9876" not in (entry["after_json"] or "")


def test_payment_without_a_bill_pays_the_oldest_bills_first(shop):
    s = shop
    old = _old_bill(s, s.ravi, 100000, 50)                              # 1,000
    mid = _old_bill(s, s.ravi, 200000, 20)                              # 2,000
    new = _credit_bill(s, qty=2)                                        # 2,000 today
    rid = receive_payment(s.conn, amount_paise=250000, mode="cash", user_id=s.till, customer_id=s.ravi)
    allocs = [(a["sale_id"], a["amount_paise"]) for a in get_receipt(s.conn, rid, user_id=s.till)["allocations"]]
    assert allocs == [(old, 100000), (mid, 150000)]                     # old fully, mid partly, newest untouched
    assert customer_outstanding_paise(s.conn, s.ravi) == 50000 + 200000
    assert get_sale(s.conn, new)["due_paise"] == 200000
    assert sum(a[1] for a in allocs) == 250000


def test_more_than_owed_or_nothing_due_is_refused_and_saves_nothing(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    with pytest.raises(ValueError, match="owes only"):
        receive_payment(s.conn, amount_paise=200001, mode="cash", user_id=s.till, sale_id=bill)
    for bad in (0, -5, 10.5, True, None):
        with pytest.raises(ValueError, match="more than zero"):
            receive_payment(s.conn, amount_paise=bad, mode="cash", user_id=s.till, sale_id=bill)
    with pytest.raises(ValueError, match="not available"):
        receive_payment(s.conn, amount_paise=100, mode="store_credit", user_id=s.till, sale_id=bill)
    with pytest.raises(ValueError, match="Nothing is due"):
        receive_payment(s.conn, amount_paise=100, mode="cash", user_id=s.till, customer_id=s.meena)
    with pytest.raises(ValueError, match="does not belong"):
        receive_payment(s.conn, amount_paise=100, mode="cash", user_id=s.till, sale_id=bill, customer_id=s.meena)
    with pytest.raises(ValueError, match="Select a customer or a bill"):
        receive_payment(s.conn, amount_paise=100, mode="cash", user_id=s.till)
    assert _count(s, "customer_receipts") == 0 and s.conn.execute(
        "SELECT last_number FROM number_series WHERE series = 'RCT'").fetchone() is None


def test_receiving_needs_the_permission_in_core(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    for user in (s.store, s.ca):
        with pytest.raises(PermissionDenied, match="receive customer payments"):
            receive_payment(s.conn, amount_paise=100, mode="cash", user_id=user, sale_id=bill)
    assert has_permission(s.conn, s.owner, "receive_customer_payment")
    assert has_permission(s.conn, s.till, "receive_customer_payment")
    set_user_permission(s.conn, s.owner, s.till, "receive_customer_payment", False)
    with pytest.raises(PermissionDenied):
        receive_payment(s.conn, amount_paise=100, mode="cash", user_id=s.till, sale_id=bill)
    set_user_permission(s.conn, s.owner, s.ca, "receive_customer_payment", True)
    receive_payment(s.conn, amount_paise=100, mode="cash", user_id=s.ca, sale_id=bill)
    assert _count(s, "customer_receipts") == 1


def test_same_idempotency_key_never_takes_the_money_twice(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    first = receive_payment(s.conn, amount_paise=50000, mode="cash", user_id=s.till, sale_id=bill, idempotency_key="k1")
    again = receive_payment(s.conn, amount_paise=50000, mode="cash", user_id=s.till, sale_id=bill, idempotency_key="k1")
    assert first == again and _count(s, "customer_receipts") == 1
    assert get_sale(s.conn, bill)["due_paise"] == 150000


def test_a_failure_midway_saves_no_receipt_no_payment_and_no_number(shop, monkeypatch):
    s = shop
    bill = _credit_bill(s, qty=2)
    payments_before = _count(s, "payments")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cc, "log_event", boom)
    with pytest.raises(RuntimeError):
        receive_payment(s.conn, amount_paise=50000, mode="cash", user_id=s.till, sale_id=bill)
    assert _count(s, "customer_receipts") == 0 and _count(s, "payments") == payments_before
    assert s.conn.execute("SELECT 1 FROM number_series WHERE series = 'RCT' AND last_number > 0").fetchone() is None
    assert get_sale(s.conn, bill)["due_paise"] == 200000


# ---------------------------------------------------------------- reversal

def test_reversal_restores_the_due_and_keeps_the_original(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    rid = receive_payment(s.conn, amount_paise=150000, mode="cheque", user_id=s.till, sale_id=bill)
    assert get_sale(s.conn, bill)["due_paise"] == 50000
    rev = reverse_receipt(s.conn, rid, user_id=s.till, reason="cheque bounced")

    assert get_sale(s.conn, bill)["due_paise"] == 200000
    original = get_receipt(s.conn, rid, user_id=s.till)
    reversal = get_receipt(s.conn, rev, user_id=s.till)
    assert original["amount_paise"] == 150000 and original["is_reversed"] is True
    assert original["reversed_by_receipt_no"] == reversal["receipt_no"]
    assert reversal["amount_paise"] == -150000 and reversal["is_reversal"] and reversal["reason"] == "cheque bounced"
    assert reversal["receipt_no"].endswith("000002") and reversal["reference"] == original["receipt_no"]
    assert reversal["allocations"][0]["amount_paise"] == -150000
    # the payments table got an opposite entry linked to the original payment
    pays = s.conn.execute("SELECT * FROM payments WHERE document_type='sale' AND document_id=? ORDER BY id", (bill,)).fetchall()
    assert [p["amount_paise"] for p in pays] == [150000, -150000]
    assert pays[1]["reversal_of"] == pays[0]["id"] and pays[1]["reason"] == "cheque bounced"
    assert [r["receipt_no"] for r in list_receipts(s.conn, s.ravi, user_id=s.till)][0] == reversal["receipt_no"]
    assert verify_chain(s.conn) == (True, None)


def test_reversal_rules(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    rid = receive_payment(s.conn, amount_paise=50000, mode="cash", user_id=s.till, sale_id=bill)
    with pytest.raises(ValueError, match="reason"):
        reverse_receipt(s.conn, rid, user_id=s.till, reason="  ")
    with pytest.raises(PermissionDenied):
        reverse_receipt(s.conn, rid, user_id=s.store, reason="x")
    with pytest.raises(ValueError, match="not found"):
        reverse_receipt(s.conn, 999, user_id=s.till, reason="x")
    rev = reverse_receipt(s.conn, rid, user_id=s.till, reason="typed wrong amount")
    with pytest.raises(ValueError, match="already reversed"):
        reverse_receipt(s.conn, rid, user_id=s.till, reason="again")
    with pytest.raises(ValueError, match="cannot be reversed"):
        reverse_receipt(s.conn, rev, user_id=s.till, reason="undo the undo")
    assert _count(s, "customer_receipts") == 2
    # the correct payment can simply be taken again
    receive_payment(s.conn, amount_paise=20000, mode="cash", user_id=s.till, sale_id=bill)
    assert get_sale(s.conn, bill)["due_paise"] == 180000


def test_reversing_a_multi_bill_receipt_restores_every_bill(shop):
    s = shop
    a = _old_bill(s, s.ravi, 100000, 40)
    b = _credit_bill(s, qty=2)
    rid = receive_payment(s.conn, amount_paise=150000, mode="cash", user_id=s.till, customer_id=s.ravi)
    assert customer_outstanding_paise(s.conn, s.ravi) == 150000
    reverse_receipt(s.conn, rid, user_id=s.till, reason="wrong customer")
    assert customer_outstanding_paise(s.conn, s.ravi) == 300000
    assert get_sale(s.conn, b)["due_paise"] == 200000
    assert get_sale(s.conn, a)["due_paise"] == 100000


# ---------------------------------------------------------------- credit limit in guarded.sell

def _sales_and_stock(s):
    return _count(s, "sales"), get_stock(s.conn, s.v)


def test_credit_bill_within_the_limit_needs_no_pin(shop):
    s = shop
    _credit_bill(s, qty=5)                                              # 5,000 = exactly the limit
    assert customer_outstanding_paise(s.conn, s.ravi) == 500000
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'credit_limit_override'").fetchone()[0] == 0


def test_bill_over_the_limit_is_refused_and_rolled_back_completely(shop):
    s = shop
    with pytest.raises(CreditLimitExceeded, match="limit is ₹5,000.00") as err:
        _credit_bill(s, qty=6)                                          # 6,000 > 5,000
    assert err.value.reason == "limit_exceeded" and err.value.limit_paise == 500000
    assert err.value.bill_due_paise == 600000 and err.value.outstanding_paise == 0
    assert _sales_and_stock(s) == (0, 50)
    assert s.conn.execute("SELECT 1 FROM number_series WHERE series = 'INV' AND last_number > 0").fetchone() is None
    assert _count(s, "payments") == 0 and _count(s, "sale_due_dates") == 0


def test_the_limit_counts_what_is_already_due(shop):
    s = shop
    _credit_bill(s, qty=3)                                              # 3,000 due
    with pytest.raises(CreditLimitExceeded):
        _credit_bill(s, qty=3)                                          # +3,000 = 6,000
    _credit_bill(s, qty=2)                                              # +2,000 = 5,000 is fine
    assert customer_outstanding_paise(s.conn, s.ravi) == 500000
    assert _count(s, "sales") == 2


def test_paying_part_now_counts_only_the_unpaid_part_against_the_limit(shop):
    s = shop
    _credit_bill(s, qty=8, cash=300000)                                 # 8,000 bill, 3,000 paid -> 5,000 due
    assert customer_outstanding_paise(s.conn, s.ravi) == 500000


def test_wrong_pin_blocks_and_right_pin_approves_and_is_logged(shop):
    s = shop
    with pytest.raises(AuthError, match="Wrong PIN"):
        _credit_bill(s, qty=6, approver_pin="0000")
    assert _sales_and_stock(s) == (0, 50)
    bill = _credit_bill(s, qty=6, approver_pin="1234")
    assert get_sale(s.conn, bill)["due_paise"] == 600000
    entry = s.conn.execute("SELECT * FROM audit_log WHERE action = 'credit_limit_override'").fetchone()
    assert entry["entity_id"] == bill and entry["user_id"] == s.till
    assert f'"approved_by": {s.owner}' in entry["after_json"] and "limit_exceeded" in entry["after_json"]
    assert verify_chain(s.conn) == (True, None)


def test_owner_signed_in_may_approve_and_it_is_still_logged(shop):
    s = shop
    bill = _credit_bill(s, qty=6, user=s.owner)
    entry = s.conn.execute("SELECT * FROM audit_log WHERE action = 'credit_limit_override'").fetchone()
    assert entry["entity_id"] == bill and f'"approved_by": {s.owner}' in entry["after_json"]


def test_customer_without_credit_is_refused_unless_the_owner_approves(shop):
    s = shop
    with pytest.raises(CreditLimitExceeded, match="not allowed for this customer") as err:
        _credit_bill(s, qty=1, customer=s.nocredit)
    assert err.value.reason == "credit_not_enabled" and _count(s, "sales") == 0
    _credit_bill(s, qty=1, customer=s.nocredit, approver_pin="1234")
    entry = s.conn.execute("SELECT after_json FROM audit_log WHERE action = 'credit_limit_override'").fetchone()
    assert "credit_not_enabled" in entry["after_json"]
    # paying in full never needs credit
    _credit_bill(s, qty=1, cash=100000, customer=s.nocredit)


def test_discount_pin_also_covers_the_credit_limit_for_the_same_bill(shop):
    s = shop
    bill = sell(
        s.conn, [{"variant_id": s.v, "qty": 6, "discount_paise": 120000}], [], user_id=s.till, customer_id=s.ravi,
        approver_pin="1234",
    )                                                                   # 20% discount (needs PIN) and 4,800 due... still within limit
    assert get_sale(s.conn, bill)["due_paise"] == 480000
    over = sell(
        s.conn, [{"variant_id": s.v, "qty": 6, "discount_paise": 60000}], [], user_id=s.till, customer_id=s.meena,
        approver_pin="1234",
    )                                                                   # 10% discount, 5,400 due: needs the PIN for credit
    assert get_sale(s.conn, over)["due_paise"] == 540000


def test_idempotent_retry_of_an_approved_credit_bill_saves_one_bill(shop):
    s = shop
    first = _credit_bill(s, qty=6, approver_pin="1234", idempotency_key="bill-1")
    again = _credit_bill(s, qty=6, approver_pin="1234", idempotency_key="bill-1")
    assert first == again and _count(s, "sales") == 1 and get_stock(s.conn, s.v) == 44


# ---------------------------------------------------------------- credit terms

def test_credit_terms_need_permission_are_validated_and_audited(shop):
    s = shop
    with pytest.raises(PermissionDenied, match="change credit terms"):
        set_credit_terms(s.conn, s.ravi, user_id=s.till, credit_enabled=True, credit_limit_paise=900000)
    for bad in (-1, 1.5, True, "100"):
        with pytest.raises(ValueError, match="Credit limit"):
            set_credit_terms(s.conn, s.ravi, user_id=s.owner, credit_enabled=True, credit_limit_paise=bad)
    for days in (-1, 366, 2.5):
        with pytest.raises(ValueError, match="Credit days"):
            set_credit_terms(s.conn, s.ravi, user_id=s.owner, credit_enabled=True, credit_limit_paise=1, default_credit_days=days)
    with pytest.raises(ValueError, match="not found"):
        set_credit_terms(s.conn, 999, user_id=s.owner, credit_enabled=True, credit_limit_paise=1)
    before = _count(s, "audit_log")
    set_credit_terms(s.conn, s.ravi, user_id=s.owner, credit_enabled=True, credit_limit_paise=500000)  # no change
    assert _count(s, "audit_log") == before
    set_credit_terms(s.conn, s.ravi, user_id=s.owner, credit_enabled=True, credit_limit_paise=900000, default_credit_days=15)
    row = s.conn.execute("SELECT credit_limit_paise, default_credit_days FROM customers WHERE id = ?", (s.ravi,)).fetchone()
    assert tuple(row) == (900000, 15)
    entry = s.conn.execute("SELECT * FROM audit_log WHERE action = 'credit_terms_changed' ORDER BY id DESC").fetchone()
    assert '"credit_limit_paise": 500000' in entry["before_json"] and '"credit_limit_paise": 900000' in entry["after_json"]


# ---------------------------------------------------------------- due dates

def test_due_date_defaults_to_bill_date_plus_credit_days_or_a_chosen_date(shop):
    s = shop
    set_credit_terms(s.conn, s.ravi, user_id=s.owner, credit_enabled=True, credit_limit_paise=500000, default_credit_days=10)
    default_bill = _credit_bill(s, qty=1)
    custom_bill = _credit_bill(s, qty=1, due_date=(TODAY + timedelta(days=3)).isoformat())
    bills = {b["sale_id"]: b for b in customer_dues(s.conn, s.ravi, user_id=s.till)["bills"]}
    assert bills[default_bill]["due_date"] == (TODAY + timedelta(days=10)).isoformat()
    assert bills[custom_bill]["due_date"] == (TODAY + timedelta(days=3)).isoformat()
    assert bills[default_bill]["overdue_days"] == 0
    with pytest.raises(ValueError, match="before the bill date"):
        _credit_bill(s, qty=1, due_date=(TODAY - timedelta(days=1)).isoformat())
    with pytest.raises(ValueError, match="valid date"):
        _credit_bill(s, qty=1, due_date="31/12/2026")
    assert _count(s, "sales") == 2                                       # refused bills left nothing behind


def test_overdue_is_measured_against_the_due_date(shop):
    s = shop
    _credit_bill(s, qty=2, due_date=(TODAY + timedelta(days=5)).isoformat())
    later = TODAY + timedelta(days=12)
    d = customer_dues(s.conn, s.ravi, user_id=s.till, as_of=later)
    assert d["bills"][0]["overdue_days"] == 7 and d["overdue_paise"] == 200000
    assert customer_dues(s.conn, s.ravi, user_id=s.till)["overdue_paise"] == 0


def test_changing_a_due_date_keeps_history_and_needs_a_reason(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    new = (TODAY + timedelta(days=45)).isoformat()
    with pytest.raises(ValueError, match="reason"):
        set_due_date(s.conn, bill, new, user_id=s.till, reason="")
    with pytest.raises(PermissionDenied):
        set_due_date(s.conn, bill, new, user_id=s.store, reason="customer asked")
    with pytest.raises(ValueError, match="before the bill date"):
        set_due_date(s.conn, bill, (TODAY - timedelta(days=1)).isoformat(), user_id=s.till, reason="x")
    set_due_date(s.conn, bill, new, user_id=s.till, reason="customer asked for time")
    assert customer_dues(s.conn, s.ravi, user_id=s.till)["bills"][0]["due_date"] == new
    history = s.conn.execute("SELECT reason FROM sale_due_dates WHERE sale_id = ? ORDER BY id", (bill,)).fetchall()
    assert [h["reason"] for h in history] == ["bill created", "customer asked for time"]
    receive_payment(s.conn, amount_paise=200000, mode="cash", user_id=s.till, sale_id=bill)
    with pytest.raises(ValueError, match="nothing due"):
        set_due_date(s.conn, bill, new, user_id=s.till, reason="again")


# ---------------------------------------------------------------- ageing and the dues report

def test_ageing_buckets_0_30_31_60_over_60(shop):
    s = shop
    _old_bill(s, s.ravi, 10000, 0)
    _old_bill(s, s.ravi, 20000, 30)
    _old_bill(s, s.ravi, 40000, 31)
    _old_bill(s, s.ravi, 80000, 60)
    _old_bill(s, s.ravi, 160000, 61)
    _old_bill(s, s.ravi, 320000, 200)
    a = customer_dues(s.conn, s.ravi, user_id=s.till)["ageing"]
    assert a == {"bucket_0_30_paise": 30000, "bucket_31_60_paise": 120000,
                 "bucket_over_60_paise": 480000, "total_paise": 630000}
    # a part-payment moves the oldest bill's remaining amount only
    receive_payment(s.conn, amount_paise=300000, mode="cash", user_id=s.till, customer_id=s.ravi)
    a = customer_dues(s.conn, s.ravi, user_id=s.till)["ageing"]
    assert a["bucket_over_60_paise"] == 180000 and a["total_paise"] == 330000


def test_dues_report_totals_sorting_filters_and_access_log(shop):
    s = shop
    _old_bill(s, s.ravi, 150000, 70)
    _credit_bill(s, qty=1, customer=s.meena, due_date=(TODAY + timedelta(days=20)).isoformat())   # 1,000, not overdue
    rep = dues_report(s.conn, user_id=s.ca)
    assert [r["customer_id"] for r in rep["rows"]] == [s.ravi, s.meena]          # biggest first
    ravi = rep["rows"][0]
    assert ravi["name"] == "Ravi" and ravi["outstanding_paise"] == 150000 and ravi["bucket_over_60_paise"] == 150000
    assert rep["totals"]["total_paise"] == 250000 and rep["totals"]["customers"] == 2
    assert rep["totals"]["bucket_0_30_paise"] == 100000 and rep["totals"]["bucket_over_60_paise"] == 150000
    assert rep["totals"]["overdue_paise"] == 150000
    overdue = dues_report(s.conn, user_id=s.till, overdue_only=True)
    assert [r["customer_id"] for r in overdue["rows"]] == [s.ravi] and overdue["totals"]["total_paise"] == 150000
    logged = s.conn.execute("SELECT customer_id, user_id FROM customer_access_log WHERE action = 'dues_report' ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in logged[:2]] == [(s.ravi, s.ca), (s.meena, s.ca)]
    with pytest.raises(PermissionDenied, match="view customer dues"):
        dues_report(s.conn, user_id=s.store)
    with pytest.raises(PermissionDenied):
        customer_dues(s.conn, s.ravi, user_id=s.store)


def test_report_matches_the_per_customer_view_and_money_is_integer(shop):
    s = shop
    _old_bill(s, s.ravi, 123457, 5)
    _credit_bill(s, qty=2, customer=s.ravi, cash=50000)
    rep = dues_report(s.conn, user_id=s.owner)
    one = customer_dues(s.conn, s.ravi, user_id=s.owner)
    assert rep["rows"][0]["outstanding_paise"] == one["outstanding_paise"] == 123457 + 150000
    for row in rep["rows"]:
        assert all(isinstance(v, int) for k, v in row.items() if k.endswith("_paise"))
    assert all(isinstance(v, int) for v in rep["totals"].values())


def test_receipts_show_up_in_the_bill_history_and_payment_reports(shop):
    s = shop
    bill = _credit_bill(s, qty=2)
    receive_payment(s.conn, amount_paise=200000, mode="upi", user_id=s.till, sale_id=bill)
    sale = get_sale(s.conn, bill)
    assert sale["is_fully_paid"] and sale["paid_paise"] == 200000
    assert s.conn.execute(
        "SELECT SUM(amount_paise) FROM payments WHERE mode = 'upi' AND document_type = 'sale'"
    ).fetchone()[0] == 200000
    assert re.fullmatch(r"RCT/\d\d-\d\d/000001", sale["payments"][0]["reference"])
