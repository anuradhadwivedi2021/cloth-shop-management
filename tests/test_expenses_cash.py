from datetime import date
from types import SimpleNamespace

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from core.audit import verify_chain
from core.cash import add_cash_movement, close_cash_day, get_cash_day, open_cash_day
from core.expenses import (
    MAX_ATTACHMENT_BYTES,
    add_expense,
    correct_expense,
    expense_summary,
    list_expenses,
    reverse_expense,
)
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.purchases import confirm_purchase, create_draft_purchase, create_supplier
from core.sales import create_sale
from core.stock import add_movement
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

TODAY = date.today().isoformat()


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('till', 'x', 'cashier')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('ca', 'x', 'accountant')")
    yield SimpleNamespace(conn=conn, owner=1, cashier=2, accountant=3, tmp=tmp_path)
    conn.close()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _rent(s, amount=500000, **kw):
    return add_expense(s.conn, kw.pop("day", "2026-11-01"), "Rent", "November rent", amount,
                       user_id=s.owner, **kw)


def _pay(conn, doc_type, amount, mode="cash"):
    """A payment row exactly as the sales, purchase and return code save it."""
    conn.execute(
        "INSERT INTO payments(document_type, document_id, mode, amount_paise) VALUES (?, 1, ?, ?)",
        (doc_type, mode, amount),
    )


# ---------- expenses ----------

def test_expense_is_saved_with_a_matching_payment_and_audit_entry(shop):
    eid = _rent(shop, payment_mode="upi", reference="UPI-123")
    row = shop.conn.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone()
    assert (row["category"], row["amount_paise"], row["payment_mode"]) == ("Rent", 500000, "upi")
    pay = shop.conn.execute("SELECT * FROM payments WHERE document_type = 'expense'").fetchone()
    assert (pay["document_id"], pay["amount_paise"], pay["mode"], pay["reference"]) == (eid, 500000, "upi", "UPI-123")
    assert pay["created_at"].startswith("2026-11-01")
    assert verify_chain(shop.conn) == (True, None)
    assert _count(shop.conn, "audit_log") == 1


def test_invalid_expense_saves_nothing(shop):
    big = shop.tmp / "big.pdf"
    big.write_bytes(b"0" * (MAX_ATTACHMENT_BYTES + 1))
    bad = [
        dict(expense_date="01-11-2026", category="Rent", description="x", amount_paise=100),
        dict(expense_date="2026-11-01", category="  ", description="x", amount_paise=100),
        dict(expense_date="2026-11-01", category="Rent", description="  ", amount_paise=100),
        dict(expense_date="2026-11-01", category="Rent", description="x", amount_paise=0),
        dict(expense_date="2026-11-01", category="Rent", description="x", amount_paise=-5),
        dict(expense_date="2026-11-01", category="Rent", description="x", amount_paise=100, payment_mode="voucher"),
        dict(expense_date="2026-11-01", category="Rent", description="x", amount_paise=100,
             attachment_path=str(shop.tmp / "missing.pdf")),
        dict(expense_date="2026-11-01", category="Rent", description="x", amount_paise=100,
             attachment_path=str(big)),
    ]
    for kwargs in bad:
        with pytest.raises(ValueError):
            add_expense(shop.conn, user_id=shop.owner, **kwargs)
    assert _count(shop.conn, "expenses") == 0
    assert _count(shop.conn, "payments") == 0
    assert _count(shop.conn, "audit_log") == 0


def test_small_receipt_attachment_is_accepted(shop):
    receipt = shop.tmp / "bill.jpg"
    receipt.write_bytes(b"0" * 1000)
    eid = _rent(shop, attachment_path=str(receipt))
    saved = shop.conn.execute("SELECT attachment_path FROM expenses WHERE id = ?", (eid,)).fetchone()[0]
    assert saved == str(receipt)


def test_only_authorised_users_can_record_or_view_expenses(shop):
    with pytest.raises(PermissionDenied):
        add_expense(shop.conn, "2026-11-01", "Rent", "x", 100, user_id=shop.cashier)
    with pytest.raises(PermissionDenied):
        add_expense(shop.conn, "2026-11-01", "Rent", "x", 100, user_id=shop.accountant)
    with pytest.raises(PermissionDenied):
        list_expenses(shop.conn, user_id=shop.cashier)
    _rent(shop)
    assert len(list_expenses(shop.conn, user_id=shop.accountant)) == 1  # accountants may view
    assert _count(shop.conn, "expenses") == 1


def test_expenses_are_never_edited_or_deleted(shop):
    _rent(shop)
    with pytest.raises(sqlcipher.DatabaseError):
        shop.conn.execute("UPDATE expenses SET amount_paise = 1")
    with pytest.raises(sqlcipher.DatabaseError):
        shop.conn.execute("DELETE FROM expenses")


def test_reversal_cancels_the_expense_and_keeps_the_history(shop):
    eid = _rent(shop)
    rid = reverse_expense(shop.conn, eid, "entered twice", user_id=shop.owner)
    rows = {r["id"]: r for r in list_expenses(shop.conn, user_id=shop.owner)}
    assert (rows[eid]["status"], rows[rid]["status"]) == ("reversed", "reversal")
    assert (rows[rid]["amount_paise"], rows[rid]["reversal_of"], rows[rid]["reason"]) == (-500000, eid, "entered twice")
    pays = shop.conn.execute("SELECT * FROM payments WHERE document_type = 'expense' ORDER BY id").fetchall()
    assert [p["amount_paise"] for p in pays] == [500000, -500000]
    assert pays[1]["reversal_of"] == pays[0]["id"]
    assert expense_summary(shop.conn, "2026-11-01", "2026-11-30", user_id=shop.owner)["total_paise"] == 0
    assert verify_chain(shop.conn) == (True, None)


def test_an_expense_can_only_be_reversed_once_and_needs_a_reason(shop):
    eid = _rent(shop)
    with pytest.raises(ValueError):
        reverse_expense(shop.conn, eid, "  ", user_id=shop.owner)
    rid = reverse_expense(shop.conn, eid, "mistake", user_id=shop.owner)
    with pytest.raises(ValueError):
        reverse_expense(shop.conn, eid, "again", user_id=shop.owner)
    with pytest.raises(ValueError):
        reverse_expense(shop.conn, rid, "undo the undo", user_id=shop.owner)
    with pytest.raises(ValueError):
        reverse_expense(shop.conn, 999, "no such", user_id=shop.owner)
    assert _count(shop.conn, "expenses") == 2


def test_correction_keeps_the_wrong_entry_and_adds_the_right_one(shop):
    wrong = _rent(shop, amount=5000000)  # typed Rs 50,000 instead of Rs 5,000
    new = correct_expense(shop.conn, wrong, "extra zero", user_id=shop.owner, amount_paise=500000)
    rows = list_expenses(shop.conn, user_id=shop.owner)
    assert len(rows) == 3
    by_id = {r["id"]: r for r in rows}
    assert by_id[wrong]["status"] == "reversed"
    assert (by_id[new]["amount_paise"], by_id[new]["corrects_id"], by_id[new]["status"]) == (500000, wrong, "active")
    assert by_id[new]["category"] == "Rent"  # fields not mentioned keep their old value
    assert expense_summary(shop.conn, "2026-11-01", "2026-11-30", user_id=shop.owner)["total_paise"] == 500000
    with pytest.raises(ValueError):
        correct_expense(shop.conn, wrong, "again", user_id=shop.owner, amount_paise=1)  # already reversed


def test_failed_correction_changes_nothing(shop):
    eid = _rent(shop)
    with pytest.raises(ValueError):
        correct_expense(shop.conn, eid, "bad amount", user_id=shop.owner, amount_paise=-1)
    assert _count(shop.conn, "expenses") == 1
    assert _count(shop.conn, "payments") == 1


def test_filters_and_summaries(shop):
    add_expense(shop.conn, "2026-10-05", "Rent", "October rent", 500000, user_id=shop.owner)
    add_expense(shop.conn, "2026-11-01", "Rent", "November rent", 500000, user_id=shop.owner)
    add_expense(shop.conn, "2026-11-01", "Electricity", "MSEB bill", 120000, user_id=shop.owner, reference="MSEB-77")
    add_expense(shop.conn, "2026-11-03", "Shopping bags", "Carry bags", 30000, user_id=shop.owner)
    owner = shop.owner
    assert len(list_expenses(shop.conn, user_id=owner)) == 4
    assert len(list_expenses(shop.conn, user_id=owner, date_from="2026-11-01")) == 3
    assert len(list_expenses(shop.conn, user_id=owner, date_from="2026-11-01", date_to="2026-11-01")) == 2
    assert len(list_expenses(shop.conn, user_id=owner, category="rent")) == 2
    assert [r["category"] for r in list_expenses(shop.conn, user_id=owner, search="mseb")] == ["Electricity"]

    by_cat = expense_summary(shop.conn, "2026-11-01", "2026-11-30", user_id=owner)
    assert by_cat["rows"] == [
        {"key": "Electricity", "total_paise": 120000},
        {"key": "Rent", "total_paise": 500000},
        {"key": "Shopping bags", "total_paise": 30000},
    ]
    assert by_cat["total_paise"] == 650000
    by_day = expense_summary(shop.conn, "2026-11-01", "2026-11-30", user_id=owner, group_by="day")
    assert [(r["key"], r["total_paise"]) for r in by_day["rows"]] == [("2026-11-01", 620000), ("2026-11-03", 30000)]
    by_month = expense_summary(shop.conn, "2026-10-01", "2026-11-30", user_id=owner, group_by="month")
    assert [(r["key"], r["total_paise"]) for r in by_month["rows"]] == [("2026-10", 500000), ("2026-11", 650000)]
    with pytest.raises(ValueError):
        expense_summary(shop.conn, "2026-11-01", "2026-11-30", user_id=owner, group_by="colour")


# ---------- cash register: opening ----------

def test_open_cash_day_rules(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=100000)
    assert get_cash_day(c, TODAY, user_id=shop.owner)["opening_paise"] == 100000
    with pytest.raises(ValueError):
        open_cash_day(c, TODAY, user_id=shop.owner)                      # same day twice
    with pytest.raises(ValueError):
        open_cash_day(c, "2099-01-01", user_id=shop.owner)               # previous day still open
    with pytest.raises(ValueError):
        open_cash_day(c, "not-a-date", user_id=shop.owner)
    assert get_cash_day(c, "2000-01-01", user_id=shop.owner)["status"] == "not_opened"


def test_only_the_owner_runs_the_cash_register_by_default(shop):
    with pytest.raises(PermissionDenied):
        open_cash_day(shop.conn, TODAY, user_id=shop.cashier)
    open_cash_day(shop.conn, TODAY, user_id=shop.owner, opening_paise=100000)
    with pytest.raises(PermissionDenied):
        add_cash_movement(shop.conn, TODAY, 1000, "float", user_id=shop.cashier)
    with pytest.raises(PermissionDenied):
        close_cash_day(shop.conn, TODAY, 100000, user_id=shop.cashier)
    assert get_cash_day(shop.conn, TODAY, user_id=shop.accountant)["status"] == "open"  # accountants may view
    with pytest.raises(PermissionDenied):
        get_cash_day(shop.conn, TODAY, user_id=shop.cashier)


# ---------- cash register: expected cash ----------

def test_expected_cash_follows_every_cash_movement(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=100000)
    _pay(c, "sale", 500000)                 # cash bill
    _pay(c, "sale", 300000, mode="upi")     # not cash: ignored
    _pay(c, "sale_return", -20000)          # cash refunded to a customer
    _pay(c, "purchase", 150000)             # cash paid to a supplier
    _pay(c, "purchase_return", -30000)      # supplier refunded cash
    add_expense(c, TODAY, "Transport", "Auto fare", 40000, user_id=shop.owner)
    add_expense(c, TODAY, "Rent", "Paid by UPI", 999900, user_id=shop.owner, payment_mode="upi")  # not cash
    add_cash_movement(c, TODAY, 10000, "small change from bank", user_id=shop.owner)
    add_cash_movement(c, TODAY, -5000, "owner took for tea", user_id=shop.owner)

    day = get_cash_day(c, TODAY, user_id=shop.owner)
    assert day["cash_sales_paise"] == 500000
    assert day["customer_refunds_paise"] == 20000
    assert day["supplier_payments_paise"] == 150000
    assert day["supplier_refunds_paise"] == 30000
    assert day["expenses_paise"] == 40000
    assert (day["manual_in_paise"], day["manual_out_paise"]) == (10000, 5000)
    assert day["expected_paise"] == 425000


def test_reversed_cash_expense_gives_the_money_back_to_the_drawer(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=100000)
    eid = add_expense(c, TODAY, "Repairs", "Fan repair", 40000, user_id=shop.owner)
    assert get_cash_day(c, TODAY, user_id=shop.owner)["expected_paise"] == 60000
    reverse_expense(c, eid, "paid by owner personally", user_id=shop.owner)
    assert get_cash_day(c, TODAY, user_id=shop.owner)["expected_paise"] == 100000


def test_real_bill_purchase_and_expense_feed_the_cash_book(shop):
    c = shop.conn
    ensure_default_tax_rules(c)
    shirt = create_product(c, "Shirt", hsn="6205", default_price_paise=105000)
    blazer = create_product(c, "Blazer", hsn="6202", default_price_paise=413000)
    (m_blue,) = generate_variants(c, shirt, ["M"], ["Blue"])
    (l_black,) = generate_variants(c, blazer, ["L"], ["Black"])
    with transaction(c):
        for v in (m_blue, l_black):
            add_movement(c, v, 10, "opening stock")
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=100000)

    create_sale(c, [{"variant_id": m_blue, "qty": 2}, {"variant_id": l_black, "qty": 1}],
                [{"mode": "cash", "amount_paise": 623000}], user_id=shop.owner, on_date=TODAY)
    supplier = create_supplier(c, "ABC Textiles")
    purchase = create_draft_purchase(
        c, supplier, "S-1", TODAY,
        [{"variant_id": m_blue, "qty": 4, "unit_cost_paise": 50000}], paid_paise=200000, created_by=shop.owner,
    )
    confirm_purchase(c, purchase, user_id=shop.owner, payment_mode="cash")
    add_expense(c, TODAY, "Packaging", "Boxes", 25000, user_id=shop.owner)

    day = get_cash_day(c, TODAY, user_id=shop.owner)
    assert day["cash_sales_paise"] == 623000
    assert day["supplier_payments_paise"] == 200000
    assert day["expenses_paise"] == 25000
    assert day["expected_paise"] == 100000 + 623000 - 200000 - 25000


# ---------- cash register: hand movements ----------

def test_cash_movement_rules(shop):
    c = shop.conn
    with pytest.raises(ValueError):
        add_cash_movement(c, TODAY, 1000, "day not open", user_id=shop.owner)
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=10000)
    with pytest.raises(ValueError):
        add_cash_movement(c, TODAY, 0, "nothing", user_id=shop.owner)
    with pytest.raises(ValueError):
        add_cash_movement(c, TODAY, 1000, "  ", user_id=shop.owner)
    with pytest.raises(ValueError):
        add_cash_movement(c, TODAY, -20000, "more than the drawer has", user_id=shop.owner)
    assert _count(c, "cash_movements") == 0
    add_cash_movement(c, TODAY, -10000, "all of it", user_id=shop.owner)
    assert get_cash_day(c, TODAY, user_id=shop.owner)["expected_paise"] == 0
    with pytest.raises(sqlcipher.DatabaseError):
        c.execute("DELETE FROM cash_movements")


# ---------- cash register: closing ----------

def test_closing_with_matching_cash(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=100000)
    _pay(c, "sale", 50000)
    result = close_cash_day(c, TODAY, 150000, user_id=shop.owner)
    assert (result["expected_paise"], result["counted_paise"], result["difference_paise"]) == (150000, 150000, 0)
    day = get_cash_day(c, TODAY, user_id=shop.owner)
    assert (day["status"], day["difference_paise"], day["changed_after_close"]) == ("closed", 0, False)


def test_a_difference_needs_a_reason_and_is_never_hidden(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=100000)
    _pay(c, "sale", 50000)
    with pytest.raises(ValueError):
        close_cash_day(c, TODAY, 149000, user_id=shop.owner)           # Rs 10 short, no reason
    assert get_cash_day(c, TODAY, user_id=shop.owner)["status"] == "open"
    result = close_cash_day(c, TODAY, 149000, user_id=shop.owner, reason="gave wrong change")
    assert result["difference_paise"] == -1000
    day = get_cash_day(c, TODAY, user_id=shop.owner)
    assert (day["expected_paise"], day["counted_paise"], day["difference_reason"]) == (150000, 149000, "gave wrong change")


def test_a_closed_day_is_final(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=0)
    close_cash_day(c, TODAY, 0, user_id=shop.owner)
    with pytest.raises(ValueError):
        close_cash_day(c, TODAY, 0, user_id=shop.owner)
    with pytest.raises(ValueError):
        add_cash_movement(c, TODAY, 1000, "late", user_id=shop.owner)
    with pytest.raises(sqlcipher.DatabaseError):
        c.execute("UPDATE cash_days SET counted_paise = 999")
    with pytest.raises(sqlcipher.DatabaseError):
        c.execute("DELETE FROM cash_days")
    with pytest.raises(ValueError):
        close_cash_day(c, "2000-01-01", 0, user_id=shop.owner)         # never opened


def test_cash_saved_after_closing_is_flagged_not_hidden(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=100000)
    close_cash_day(c, TODAY, 100000, user_id=shop.owner)
    _pay(c, "sale", 7000)   # a cash payment that lands in the already-closed day
    day = get_cash_day(c, TODAY, user_id=shop.owner)
    assert day["changed_after_close"] is True
    assert day["closed_expected_paise"] == 100000   # the closed figure is untouched
    assert day["expected_paise"] == 107000


def test_next_day_opens_with_the_cash_counted_at_the_last_close(shop):
    c = shop.conn
    open_cash_day(c, "2026-11-01", user_id=shop.owner, opening_paise=100000)
    close_cash_day(c, "2026-11-01", 123400, user_id=shop.owner, reason="counted extra Rs 234")
    open_cash_day(c, "2026-11-02", user_id=shop.owner)
    assert get_cash_day(c, "2026-11-02", user_id=shop.owner)["opening_paise"] == 123400
    close_cash_day(c, "2026-11-02", 123400, user_id=shop.owner)
    open_cash_day(c, "2026-11-03", user_id=shop.owner, opening_paise=50000)   # owner may set it
    assert get_cash_day(c, "2026-11-03", user_id=shop.owner)["opening_paise"] == 50000


def test_cash_register_actions_are_in_the_audit_log(shop):
    c = shop.conn
    open_cash_day(c, TODAY, user_id=shop.owner, opening_paise=0)
    add_cash_movement(c, TODAY, 5000, "float", user_id=shop.owner)
    close_cash_day(c, TODAY, 5000, user_id=shop.owner)
    actions = [r["action"] for r in c.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["cash_day_opened", "cash_movement", "cash_day_closed"]
    assert verify_chain(c) == (True, None)
