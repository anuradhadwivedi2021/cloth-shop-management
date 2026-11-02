from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import auth
from core.audit import verify_chain
from core.cash import get_cash_day, open_cash_day
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.purchase_returns import process_purchase_return
from core.purchases import confirm_purchase, create_draft_purchase, create_supplier, get_purchase
from core.supplier_ledger import (
    all_supplier_balances, list_supplier_payments, pay_supplier, reverse_supplier_payment,
    search_suppliers, set_opening_balance, set_supplier_active, supplier_balance, supplier_ledger,
    supplier_outstanding_bills, update_supplier,
)
from db.connection import open_db

TODAY = date.today().isoformat()
GSTIN = "27AAPFU0939F1ZV"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")
    supplier = create_supplier(conn, "Ravi Textiles", phone="9000000001")
    pid = create_product(conn, "Shirt")
    m, l = generate_variants(conn, pid, ["M", "L"], ["Blue"])
    yield SimpleNamespace(conn=conn, owner=owner, till=till, store=store, ca=ca, supplier=supplier, m=m, l=l)
    conn.close()


def _bill(s, *, paid=0, qty=10, day=TODAY, supplier=None, invoice="INV-1"):
    """qty x M-Blue at Rs 500 with 5% tax: Rs 525 per piece, so Rs 5,250 for 10."""
    pid = create_draft_purchase(
        s.conn, supplier or s.supplier, invoice, day,
        [{"variant_id": s.m, "qty": qty, "unit_cost_paise": 50000, "tax_rate_percent": 5}],
        paid_paise=paid, created_by=s.owner,
    )
    confirm_purchase(s.conn, pid, user_id=s.owner, payment_mode="cash")
    return pid


def _bal(s, supplier=None):
    return supplier_balance(s.conn, supplier or s.supplier, user_id=s.owner)["balance_paise"]


# ---------- balances ----------

def test_balance_is_purchases_minus_payments(shop):
    assert _bal(shop) == 0
    pid = _bill(shop, paid=100000)  # total 525,000 paise; paid 100,000
    assert _bal(shop) == 425000
    pay_supplier(shop.conn, shop.owner, shop.supplier, 125000, "upi", purchase_id=pid)
    assert _bal(shop) == 300000
    parts = supplier_balance(shop.conn, shop.supplier, user_id=shop.owner)
    assert (parts["purchases_paise"], parts["paid_on_bills_paise"], parts["paid_on_account_paise"]) == (525000, 225000, 0)


def test_opening_balance_counts_and_is_locked_after_activity(shop):
    set_opening_balance(shop.conn, shop.owner, shop.supplier, 80000)
    assert _bal(shop) == 80000
    set_opening_balance(shop.conn, shop.owner, shop.supplier, 90000)  # still allowed: nothing happened yet
    pid = _bill(shop)
    assert _bal(shop) == 90000 + 525000
    with pytest.raises(ValueError, match="cannot be changed"):
        set_opening_balance(shop.conn, shop.owner, shop.supplier, 0)
    assert pid


def test_balance_after_purchase_return_matches_the_documents(shop):
    # unpaid bill: returning goods reduces what we owe
    pid = _bill(shop)
    _, lines = get_purchase(shop.conn, pid)
    process_purchase_return(shop.conn, pid, [{"purchase_line_id": lines[0]["id"], "qty": 4}], user_id=shop.owner, reason="r")
    assert _bal(shop) == 525000 - 210000
    # paid in full: a cash refund brings the balance back to zero, a credit leaves them owing us
    pid2 = _bill(shop, paid=525000, invoice="INV-2")
    _, l2 = get_purchase(shop.conn, pid2)
    process_purchase_return(shop.conn, pid2, [{"purchase_line_id": l2[0]["id"], "qty": 2}], user_id=shop.owner, reason="r")
    assert _bal(shop) == 525000 - 210000  # second bill: paid 525,000 - debit 105,000 + refund 105,000 = 0 change
    pid3 = _bill(shop, paid=525000, invoice="INV-3")
    _, l3 = get_purchase(shop.conn, pid3)
    process_purchase_return(
        shop.conn, pid3, [{"purchase_line_id": l3[0]["id"], "qty": 2}],
        user_id=shop.owner, reason="r", settlement="credit",
    )
    assert _bal(shop) == 525000 - 210000 - 105000  # they now owe us a credit of 105,000


# ---------- paying ----------

def test_pay_against_bill_and_overpay_blocked(shop):
    pid = _bill(shop)
    sp = pay_supplier(shop.conn, shop.owner, shop.supplier, 200000, "bank_transfer", purchase_id=pid, reference="UTR123")
    row = shop.conn.execute("SELECT * FROM supplier_payments WHERE id = ?", (sp,)).fetchone()
    assert row["voucher_no"].startswith("SPV/") and row["voucher_no"].endswith("000001") and row["reference"] == "UTR123"
    pay = shop.conn.execute("SELECT * FROM payments WHERE id = ?", (row["payment_id"],)).fetchone()
    assert (pay["document_type"], pay["document_id"], pay["amount_paise"], pay["mode"]) == ("purchase", pid, 200000, "bank_transfer")
    with pytest.raises(ValueError, match="only has 3,250.00 left"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, 325001, "cash", purchase_id=pid)
    pay_supplier(shop.conn, shop.owner, shop.supplier, 325000, "cash", purchase_id=pid)
    assert supplier_outstanding_bills(shop.conn, shop.supplier, user_id=shop.owner) == []
    with pytest.raises(ValueError, match="left to pay"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, 1, "cash", purchase_id=pid)


def test_bill_owes_less_after_a_return(shop):
    pid = _bill(shop)
    _, lines = get_purchase(shop.conn, pid)
    process_purchase_return(shop.conn, pid, [{"purchase_line_id": lines[0]["id"], "qty": 4}], user_id=shop.owner, reason="r")
    (bill,) = supplier_outstanding_bills(shop.conn, shop.supplier, user_id=shop.owner)
    assert bill["returns_credit_paise"] == 210000 and bill["outstanding_paise"] == 315000
    with pytest.raises(ValueError, match="left to pay"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, 315001, "cash", purchase_id=pid)


def test_advance_on_account_reduces_balance_but_not_a_bill(shop):
    pid = _bill(shop)
    pay_supplier(shop.conn, shop.owner, shop.supplier, 100000, "upi")  # no purchase_id
    assert _bal(shop) == 425000
    (bill,) = supplier_outstanding_bills(shop.conn, shop.supplier, user_id=shop.owner)
    assert bill["outstanding_paise"] == 525000  # bill itself is untouched
    pay_supplier(shop.conn, shop.owner, shop.supplier, 900000, "upi")  # an advance may exceed what is owed
    assert _bal(shop) == 525000 - 1000000
    assert pid


@pytest.mark.parametrize("amount", [0, -5, 1.5, "100", None, True])
def test_bad_amounts_refused(shop, amount):
    with pytest.raises(ValueError, match="greater than zero"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, amount, "cash")


def test_bad_inputs_refused(shop):
    pid = _bill(shop)
    with pytest.raises(ValueError, match="payment method"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, 100, "gold")
    with pytest.raises(ValueError, match="future"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, 100, "cash", payment_date=(date.today() + timedelta(days=1)))
    with pytest.raises(ValueError, match="valid date"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, 100, "cash", payment_date="31/12/2026")
    with pytest.raises(ValueError, match="Supplier not found"):
        pay_supplier(shop.conn, shop.owner, 999, 100, "cash")
    other = create_supplier(shop.conn, "Other Supplier")
    with pytest.raises(ValueError, match="does not belong"):
        pay_supplier(shop.conn, shop.owner, other, 100, "cash", purchase_id=pid)
    draft = create_draft_purchase(
        shop.conn, shop.supplier, "D-1", TODAY,
        [{"variant_id": shop.m, "qty": 1, "unit_cost_paise": 100, "tax_rate_percent": 0}], created_by=shop.owner,
    )
    with pytest.raises(ValueError, match="confirmed"):
        pay_supplier(shop.conn, shop.owner, shop.supplier, 50, "cash", purchase_id=draft)
    assert shop.conn.execute("SELECT COUNT(*) FROM supplier_payments").fetchone()[0] == 0


def test_idempotency_key_prevents_double_payment(shop):
    pid = _bill(shop)
    a = pay_supplier(shop.conn, shop.owner, shop.supplier, 100000, "cash", purchase_id=pid, idempotency_key="k1")
    b = pay_supplier(shop.conn, shop.owner, shop.supplier, 100000, "cash", purchase_id=pid, idempotency_key="k1")
    assert a == b and _bal(shop) == 425000


def test_backdated_payment_keeps_its_date(shop):
    day = (date.today() - timedelta(days=3)).isoformat()
    sp = pay_supplier(shop.conn, shop.owner, shop.supplier, 5000, "upi", payment_date=day)
    row = shop.conn.execute("SELECT payment_id, payment_date FROM supplier_payments WHERE id = ?", (sp,)).fetchone()
    assert row["payment_date"] == day
    assert shop.conn.execute("SELECT created_at FROM payments WHERE id = ?", (row["payment_id"],)).fetchone()[0].startswith(day)


def test_voucher_numbers_are_sequential(shop):
    ids = [pay_supplier(shop.conn, shop.owner, shop.supplier, 100, "cash") for _ in range(3)]
    nums = [shop.conn.execute("SELECT voucher_no FROM supplier_payments WHERE id = ?", (i,)).fetchone()[0] for i in ids]
    assert [n[-6:] for n in nums] == ["000001", "000002", "000003"]


# ---------- reversal ----------

def test_reverse_payment_restores_the_balance_and_keeps_the_original(shop):
    pid = _bill(shop)
    sp = pay_supplier(shop.conn, shop.owner, shop.supplier, 200000, "cash", purchase_id=pid)
    assert _bal(shop) == 325000
    rev = reverse_supplier_payment(shop.conn, shop.owner, sp, "paid wrong supplier")
    assert _bal(shop) == 525000
    (bill,) = supplier_outstanding_bills(shop.conn, shop.supplier, user_id=shop.owner)
    assert bill["outstanding_paise"] == 525000
    rows = list_supplier_payments(shop.conn, shop.supplier, user_id=shop.owner)
    assert [r["amount_paise"] for r in rows] == [200000, -200000] and rows[1]["reversal_of"] == sp and rows[1]["id"] == rev
    neg = shop.conn.execute("SELECT * FROM payments WHERE amount_paise = -200000").fetchone()
    assert neg["document_type"] == "purchase" and neg["reversal_of"] == rows[0]["payment_id"]
    with pytest.raises(ValueError, match="already been reversed"):
        reverse_supplier_payment(shop.conn, shop.owner, sp, "again")
    with pytest.raises(ValueError, match="cannot be reversed"):
        reverse_supplier_payment(shop.conn, shop.owner, rev, "undo undo")
    with pytest.raises(ValueError, match="reason"):
        reverse_supplier_payment(shop.conn, shop.owner, 999, " ")
    pay_supplier(shop.conn, shop.owner, shop.supplier, 525000, "cash", purchase_id=pid)  # bill can be paid again
    assert _bal(shop) == 0


def test_reversing_an_advance(shop):
    sp = pay_supplier(shop.conn, shop.owner, shop.supplier, 70000, "upi")
    reverse_supplier_payment(shop.conn, shop.owner, sp, "bounced")
    assert _bal(shop) == 0


def test_payments_cannot_be_edited_or_deleted_in_the_database(shop):
    pay_supplier(shop.conn, shop.owner, shop.supplier, 100, "cash")
    with pytest.raises(Exception, match="cannot be edited"):
        shop.conn.execute("UPDATE supplier_payments SET amount_paise = 5")
    with pytest.raises(Exception, match="cannot be deleted"):
        shop.conn.execute("DELETE FROM supplier_payments")
    with pytest.raises(Exception, match="never deleted"):
        shop.conn.execute("DELETE FROM suppliers")


# ---------- cash register ----------

def test_cash_payment_reduces_expected_drawer_cash_but_upi_does_not(shop):
    open_cash_day(shop.conn, TODAY, user_id=shop.owner, opening_paise=1000000)
    pid = _bill(shop)
    pay_supplier(shop.conn, shop.owner, shop.supplier, 200000, "cash", purchase_id=pid)
    pay_supplier(shop.conn, shop.owner, shop.supplier, 50000, "cash")  # advance, also from the drawer
    pay_supplier(shop.conn, shop.owner, shop.supplier, 300000, "upi", purchase_id=pid)
    day = get_cash_day(shop.conn, TODAY, user_id=shop.owner)
    assert day["supplier_payments_paise"] == 250000 and day["expected_paise"] == 1000000 - 250000
    rev = shop.conn.execute("SELECT id FROM supplier_payments WHERE amount_paise = 200000").fetchone()[0]
    reverse_supplier_payment(shop.conn, shop.owner, rev, "wrong")
    assert get_cash_day(shop.conn, TODAY, user_id=shop.owner)["expected_paise"] == 1000000 - 50000


# ---------- ledger ----------

def test_ledger_lists_everything_with_a_running_balance(shop):
    day1 = (date.today() - timedelta(days=5)).isoformat()
    set_opening_balance(shop.conn, shop.owner, shop.supplier, 10000)
    pid = _bill(shop, paid=100000, day=day1)
    _, lines = get_purchase(shop.conn, pid)
    pay_supplier(shop.conn, shop.owner, shop.supplier, 50000, "upi")
    process_purchase_return(shop.conn, pid, [{"purchase_line_id": lines[0]["id"], "qty": 2}], user_id=shop.owner, reason="r")
    led = supplier_ledger(shop.conn, shop.supplier, user_id=shop.owner)
    kinds = [e["kind"] for e in led["entries"]]
    assert kinds == ["purchase", "payment on bill", "advance payment", "debit note"]
    assert led["opening_paise"] == 10000
    assert [e["balance_paise"] for e in led["entries"]] == [535000, 435000, 385000, 280000]
    assert led["closing_paise"] == 280000 == _bal(shop)
    assert led["entries"][0]["reference"].startswith("PV/") or "INV-1" in led["entries"][0]["reference"]


def test_ledger_closing_always_equals_balance_with_all_event_types(shop):
    pid = _bill(shop, paid=525000)
    _, lines = get_purchase(shop.conn, pid)
    process_purchase_return(shop.conn, pid, [{"purchase_line_id": lines[0]["id"], "qty": 3}], user_id=shop.owner, reason="r")  # refund
    sp = pay_supplier(shop.conn, shop.owner, shop.supplier, 40000, "cash")
    reverse_supplier_payment(shop.conn, shop.owner, sp, "oops")
    _bill(shop, invoice="INV-2")
    led = supplier_ledger(shop.conn, shop.supplier, user_id=shop.owner)
    assert led["closing_paise"] == _bal(shop)
    assert "refund received" in [e["kind"] for e in led["entries"]]
    assert "advance payment reversed" in [e["kind"] for e in led["entries"]]


def test_ledger_date_range_brings_balance_forward(shop):
    old = (date.today() - timedelta(days=40)).isoformat()
    _bill(shop, day=old)
    pay_supplier(shop.conn, shop.owner, shop.supplier, 100000, "upi")
    start = (date.today() - timedelta(days=10)).isoformat()
    led = supplier_ledger(shop.conn, shop.supplier, user_id=shop.owner, date_from=start)
    assert led["opening_paise"] == 525000 and [e["kind"] for e in led["entries"]] == ["advance payment"]
    assert led["closing_paise"] == 425000
    led2 = supplier_ledger(shop.conn, shop.supplier, user_id=shop.owner, date_to=(date.today() - timedelta(days=20)).isoformat())
    assert led2["closing_paise"] == 525000 and len(led2["entries"]) == 1


def test_outstanding_bills_with_ageing(shop):
    _bill(shop, day=(date.today() - timedelta(days=100)).isoformat(), invoice="OLD")
    _bill(shop, day=(date.today() - timedelta(days=45)).isoformat(), invoice="MID")
    _bill(shop, day=TODAY, invoice="NEW")
    bills = supplier_outstanding_bills(shop.conn, shop.supplier, user_id=shop.owner)
    assert [(b["invoice_no"], b["age_bucket"]) for b in bills] == [("OLD", "over 90"), ("MID", "31-60"), ("NEW", "0-30")]


def test_all_balances_sorted_largest_first_and_zero_hidden(shop):
    other = create_supplier(shop.conn, "Big Mills")
    _bill(shop)
    _bill(shop, qty=20, supplier=other, invoice="B-1")
    create_supplier(shop.conn, "Quiet Supplier")
    rows = all_supplier_balances(shop.conn, user_id=shop.owner)
    assert [r["name"] for r in rows] == ["Big Mills", "Ravi Textiles"]
    assert len(all_supplier_balances(shop.conn, user_id=shop.owner, include_zero=True)) == 3


# ---------- supplier details ----------

def test_update_supplier_keeps_old_purchases_and_logs_before_after(shop):
    pid = _bill(shop)
    changed = update_supplier(
        shop.conn, shop.store, shop.supplier, phone="9111111111", gstin=" 27aapfu0939f1zv ",
        account_reference="ACC-77", name="Ravi Textiles Pvt",
    )
    assert changed == ["account_reference", "gstin", "name", "phone"]
    s = shop.conn.execute("SELECT * FROM suppliers WHERE id = ?", (shop.supplier,)).fetchone()
    assert (s["gstin"], s["account_reference"], s["updated_at"] is not None) == (GSTIN, "ACC-77", True)
    assert shop.conn.execute("SELECT supplier_id, total_paise FROM purchases WHERE id = ?", (pid,)).fetchone()[:] == (shop.supplier, 525000)
    assert update_supplier(shop.conn, shop.store, shop.supplier, phone="9111111111") == []
    assert verify_chain(shop.conn) == (True, None)


def test_update_supplier_validation(shop):
    with pytest.raises(ValueError, match="GSTIN"):
        update_supplier(shop.conn, shop.owner, shop.supplier, gstin="123")
    with pytest.raises(ValueError, match="name is required"):
        update_supplier(shop.conn, shop.owner, shop.supplier, name="  ")
    with pytest.raises(ValueError, match="Cannot change"):
        update_supplier(shop.conn, shop.owner, shop.supplier, opening_balance_paise=5)
    with pytest.raises(ValueError, match="not found"):
        update_supplier(shop.conn, shop.owner, 999, phone="1")
    update_supplier(shop.conn, shop.owner, shop.supplier, gstin="")  # blank clears it
    assert shop.conn.execute("SELECT gstin FROM suppliers WHERE id = ?", (shop.supplier,)).fetchone()[0] is None


def test_deactivated_supplier_keeps_history_and_can_still_be_paid(shop):
    pid = _bill(shop)
    set_supplier_active(shop.conn, shop.owner, shop.supplier, False)
    assert search_suppliers(shop.conn, user_id=shop.owner, query="Ravi") == []
    assert len(search_suppliers(shop.conn, user_id=shop.owner, query="Ravi", include_inactive=True)) == 1
    pay_supplier(shop.conn, shop.owner, shop.supplier, 525000, "cash", purchase_id=pid)  # dues must stay payable
    assert _bal(shop) == 0
    set_supplier_active(shop.conn, shop.owner, shop.supplier, True)
    assert len(search_suppliers(shop.conn, user_id=shop.owner, query="9000000001")) == 1


# ---------- permissions ----------

def test_permissions(shop):
    pid = _bill(shop)
    for user in (shop.till, shop.store, shop.ca):
        with pytest.raises(PermissionDenied):
            pay_supplier(shop.conn, user, shop.supplier, 100, "cash", purchase_id=pid)
        with pytest.raises(PermissionDenied):
            set_opening_balance(shop.conn, user, shop.supplier, 5)
    with pytest.raises(PermissionDenied):
        update_supplier(shop.conn, shop.till, shop.supplier, phone="1")
    with pytest.raises(PermissionDenied):
        update_supplier(shop.conn, shop.ca, shop.supplier, phone="1")  # accountant is read-only
    with pytest.raises(PermissionDenied):
        supplier_balance(shop.conn, shop.supplier, user_id=shop.till)  # cashier cannot see what we owe
    assert supplier_balance(shop.conn, shop.supplier, user_id=shop.ca)["balance_paise"] == 525000
    assert supplier_balance(shop.conn, shop.supplier, user_id=shop.store)["balance_paise"] == 525000
    with pytest.raises(PermissionDenied):
        reverse_supplier_payment(shop.conn, shop.ca, 1, "x")


def test_audit_chain_stays_valid_and_has_no_secrets(shop):
    pid = _bill(shop)
    sp = pay_supplier(shop.conn, shop.owner, shop.supplier, 100000, "cash", purchase_id=pid)
    reverse_supplier_payment(shop.conn, shop.owner, sp, "wrong")
    actions = {r[0] for r in shop.conn.execute("SELECT action FROM audit_log")}
    assert {"supplier_payment", "supplier_payment_reversed"} <= actions
    assert verify_chain(shop.conn) == (True, None)
