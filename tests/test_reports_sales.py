from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import auth
from core.customers import create_customer
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.report_common import resolve_period
from core.reports_sales import (
    day_book, gst_sales_register, payment_mode_summary, reconcile, sales_by, sales_summary, series_gap_check,
)
from core.returns import process_return
from core.sales import create_sale
from core.stock import add_movement
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

TODAY = date.today().isoformat()
GSTIN = "27AAPFU0939F1ZV"
CASH = [{"mode": "cash", "amount_paise": 10 ** 7}]


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000, category="Shirts", brand="Raymond")
    blazer = create_product(conn, "Blazer", hsn="6203", default_price_paise=413000, category="Suits", brand="Peter")
    (shirt_m,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    (blazer_l,) = generate_variants(conn, blazer, ["L"], ["Black"])
    with transaction(conn):
        for v in (shirt_m, blazer_l):
            add_movement(conn, v, 50, "opening stock")
    yield SimpleNamespace(conn=conn, owner=owner, till=till, ca=ca, store=store, shirt=shirt_m, blazer=blazer_l)
    conn.close()


def _sell(s, shirts=0, blazers=0, *, user=None, customer=None, mode="regular", payments=CASH, **kw):
    cart = []
    if shirts:
        cart.append({"variant_id": s.shirt, "qty": shirts})
    if blazers:
        cart.append({"variant_id": s.blazer, "qty": blazers})
    return create_sale(s.conn, cart, payments, user_id=user or s.till, customer_id=customer, mode=mode, **kw)


def _move_sale(s, sale_id, day, time="12:00:00"):
    """Pretend a bill was saved on another day (tests only: the real triggers forbid editing)."""
    s.conn.execute("DROP TRIGGER IF EXISTS sales_no_edit")
    s.conn.execute("UPDATE sales SET created_at = ? WHERE id = ?", (f"{day} {time}", sale_id))
    s.conn.execute("DROP TRIGGER IF EXISTS payments_no_update")
    s.conn.execute(
        "UPDATE payments SET created_at = ? WHERE document_type = 'sale' AND document_id = ?", (f"{day} {time}", sale_id)
    )


def _summary(s, a=TODAY, b=TODAY, user=None):
    return sales_summary(s.conn, a, b, user_id=user or s.owner)


# ---------- periods ----------

def test_periods():
    today = date(2026, 11, 4)  # a Wednesday
    assert resolve_period("today", today=today) == ("2026-11-04", "2026-11-04")
    assert resolve_period("yesterday", today=today) == ("2026-11-03", "2026-11-03")
    assert resolve_period("this_week", today=today) == ("2026-11-02", "2026-11-04")
    assert resolve_period("this_month", today=today) == ("2026-11-01", "2026-11-04")
    assert resolve_period("this_fin_year", today=today) == ("2026-04-01", "2026-11-04")
    assert resolve_period("this_fin_year", today=date(2027, 2, 10)) == ("2026-04-01", "2027-02-10")
    assert resolve_period("custom", date_from="2026-01-05", date_to="2026-01-09") == ("2026-01-05", "2026-01-09")
    with pytest.raises(ValueError, match="cannot be after"):
        resolve_period("custom", date_from="2026-02-01", date_to="2026-01-01")
    with pytest.raises(ValueError, match="valid date"):
        resolve_period("custom", date_from="01/02/2026", date_to="2026-01-01")
    with pytest.raises(ValueError, match="Period must be"):
        resolve_period("last_decade")


# ---------- summary ----------

def test_summary_reproduces_the_srs_worked_example(shop):
    _sell(shop, shirts=2, blazers=1)  # Rs 2,100 + Rs 4,130 tax inclusive
    s = _summary(shop)
    assert (s["bill_count"], s["items_sold"]) == (1, 3)
    assert (s["taxable_paise"], s["tax_paise"], s["invoice_total_paise"]) == (550000, 73000, 623000)
    assert s["average_bill_paise"] == 623000
    assert s["net_sales_paise"] == 550000 and "before tax" in s["formula"]


def test_credit_notes_reduce_net_sales(shop):
    sale = _sell(shop, shirts=2, blazers=1)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ? AND variant_id = ?", (sale, shop.shirt)).fetchone()[0]
    process_return(shop.conn, sale, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, reason="wrong_size")
    s = _summary(shop)
    assert (s["credit_note_count"], s["items_returned"]) == (1, 1)
    assert (s["credit_notes_taxable_paise"], s["credit_notes_tax_paise"], s["credit_notes_total_paise"]) == (100000, 5000, 105000)
    assert s["refunds_paise"] == 105000
    assert s["net_sales_paise"] == 450000 and s["net_tax_paise"] == 68000
    assert s["invoice_total_paise"] == 623000  # the original bill is never reduced


def test_exchange_is_counted_as_return_plus_new_sale(shop):
    sale = _sell(shop, shirts=1)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ?", (sale,)).fetchone()[0]
    process_return(
        shop.conn, sale, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, reason="wrong_size",
        outcome="exchange", exchange_cart=[{"variant_id": shop.shirt, "qty": 1}], payments=CASH,
    )
    s = _summary(shop)
    assert s["bill_count"] == 2 and s["exchange_count"] == 1
    assert s["net_sales_paise"] == 100000  # one shirt kept in the end


def test_summary_respects_the_date_range_and_empty_period(shop):
    old = _sell(shop, shirts=1)
    _move_sale(shop, old, (date.today() - timedelta(days=10)).isoformat())
    _sell(shop, shirts=1)
    assert _summary(shop)["bill_count"] == 1
    wide = _summary(shop, (date.today() - timedelta(days=30)).isoformat(), TODAY)
    assert wide["bill_count"] == 2 and wide["taxable_paise"] == 200000
    empty = _summary(shop, "2020-01-01", "2020-01-31")
    assert empty["bill_count"] == 0 and empty["average_bill_paise"] == 0 and empty["net_sales_paise"] == 0


def test_payment_mode_summary_is_money_not_sales_value(shop):
    sale = _sell(shop, shirts=2, blazers=1, payments=[
        {"mode": "cash", "amount_paise": 400000}, {"mode": "upi", "amount_paise": 223000, "reference": "U1"},
    ])
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ? AND variant_id = ?", (sale, shop.shirt)).fetchone()[0]
    process_return(shop.conn, sale, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, reason="x", refund_mode="cash")
    rows = {r["mode"]: r for r in payment_mode_summary(shop.conn, TODAY, TODAY, user_id=shop.owner)}
    assert rows["cash"]["received_paise"] == 400000 and rows["cash"]["refunded_paise"] == 105000
    assert rows["cash"]["net_paise"] == 295000
    assert rows["upi"]["received_paise"] == 223000 and rows["upi"]["refunded_paise"] == 0


# ---------- grouped ----------

def test_sales_by_product_variant_category_brand(shop):
    sale = _sell(shop, shirts=2, blazers=1)
    _sell(shop, shirts=1)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ? AND variant_id = ?", (sale, shop.shirt)).fetchone()[0]
    process_return(shop.conn, sale, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, reason="x")
    by_product = sales_by(shop.conn, TODAY, TODAY, "product", user_id=shop.owner)
    assert [r["key"] for r in by_product] == ["Blazer", "Shirt"]  # largest taxable first
    shirt = by_product[1]
    assert (shirt["bills"], shirt["qty"], shirt["taxable_paise"]) == (2, 3, 300000)
    assert (shirt["returned_qty"], shirt["returned_taxable_paise"], shirt["net_taxable_paise"]) == (1, 100000, 200000)
    assert sales_by(shop.conn, TODAY, TODAY, "variant", user_id=shop.owner)[1]["key"] == "Shirt | M | Blue"
    assert {r["key"] for r in sales_by(shop.conn, TODAY, TODAY, "category", user_id=shop.owner)} == {"Shirts", "Suits"}
    assert {r["key"] for r in sales_by(shop.conn, TODAY, TODAY, "brand", user_id=shop.owner)} == {"Raymond", "Peter"}
    # grouped totals always add up to the summary
    total = sum(r["net_taxable_paise"] for r in by_product)
    assert total == _summary(shop)["net_sales_paise"]


def test_sales_by_day_hour_salesperson_and_payment_mode(shop):
    a = _sell(shop, shirts=1, user=shop.till)
    b = _sell(shop, blazers=1, user=shop.owner, salesperson_id=shop.owner)
    c = _sell(shop, shirts=1, user=shop.till)
    _move_sale(shop, a, (date.today() - timedelta(days=1)).isoformat(), "10:15:00")
    _move_sale(shop, c, (date.today() - timedelta(days=1)).isoformat(), "10:45:00")
    _move_sale(shop, b, (date.today() - timedelta(days=1)).isoformat(), "17:05:00")
    start = (date.today() - timedelta(days=1)).isoformat()
    days = sales_by(shop.conn, start, TODAY, "day", user_id=shop.owner)
    assert [(r["key"], r["bills"]) for r in days] == [(start, 3)]
    hours = sales_by(shop.conn, start, TODAY, "hour", user_id=shop.owner)
    assert [(r["key"], r["bills"]) for r in hours] == [("10", 2), ("17", 1)]
    people = {r["key"]: r for r in sales_by(shop.conn, start, TODAY, "salesperson", user_id=shop.owner)}
    assert people["till"]["bills"] == 2 and people["Anuradha"]["bills"] == 1
    modes = sales_by(shop.conn, start, TODAY, "payment_mode", user_id=shop.owner)
    assert modes[0]["key"] == "cash" and modes[0]["bills"] == 3 and modes[0]["total_paise"] == 105000 * 2 + 413000
    with pytest.raises(ValueError, match="Group by"):
        sales_by(shop.conn, TODAY, TODAY, "colour", user_id=shop.owner)


# ---------- day book ----------

def test_day_book_lists_documents_in_order_with_totals(shop):
    from core.expenses import add_expense
    from core.purchases import confirm_purchase, create_draft_purchase, create_supplier
    sale = _sell(shop, shirts=1)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ?", (sale,)).fetchone()[0]
    process_return(shop.conn, sale, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, reason="x")
    sup = create_supplier(shop.conn, "Ravi Textiles")
    pid = create_draft_purchase(
        shop.conn, sup, "INV-9", TODAY,
        [{"variant_id": shop.shirt, "qty": 10, "unit_cost_paise": 50000, "tax_rate_percent": 5}], created_by=shop.owner,
    )
    confirm_purchase(shop.conn, pid, user_id=shop.owner, payment_mode="cash")
    add_expense(shop.conn, TODAY, "Rent", "Shop rent", 1500000, user_id=shop.owner)
    book = day_book(shop.conn, TODAY, TODAY, user_id=shop.owner)
    kinds = {e["type"] for e in book["entries"]}
    assert kinds == {"sale", "credit_note", "purchase", "expense"}
    t = book["totals"]
    assert t["sale"]["total_paise"] == 105000 and t["credit_note"]["total_paise"] == 105000
    assert t["purchase"]["total_paise"] == 525000 and t["expense"]["total_paise"] == 1500000
    assert t["debit_note"]["count"] == 0
    assert all("customer" not in k and "phone" not in k for e in book["entries"] for k in e)
    assert [e["type"] for e in book["entries"]].index("sale") < [e["type"] for e in book["entries"]].index("credit_note")


def test_cancelled_bills_are_listed_but_not_totalled(shop):
    sale = _sell(shop, shirts=1)
    shop.conn.execute("UPDATE sales SET status = 'cancelled' WHERE id = ?", (sale,))
    book = day_book(shop.conn, TODAY, TODAY, user_id=shop.owner)
    assert book["entries"][0]["note"] == "cancelled" and book["totals"]["sale"]["count"] == 0
    s = _summary(shop)
    assert s["bill_count"] == 0 and s["cancelled_bills"] == 1


# ---------- reconcile and gaps ----------

def test_reconcile_is_clean_for_normal_data(shop):
    sale = _sell(shop, shirts=2, blazers=1)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ? AND variant_id = ?", (sale, shop.blazer)).fetchone()[0]
    process_return(shop.conn, sale, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, reason="x")
    for _ in range(3):
        _sell(shop, shirts=3)
    result = reconcile(shop.conn, TODAY, TODAY, user_id=shop.owner)
    assert result == {"ok": True, "problems": [], "unpaid_bills": []}


def test_reconcile_finds_a_tampered_bill(shop):
    sale = _sell(shop, shirts=1)
    invoice = shop.conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (sale,)).fetchone()[0]
    shop.conn.execute("DROP TRIGGER sale_lines_no_update")
    shop.conn.execute("UPDATE sale_lines SET taxable_paise = taxable_paise + 100 WHERE sale_id = ?", (sale,))
    result = reconcile(shop.conn, TODAY, TODAY, user_id=shop.owner)
    assert not result["ok"] and any(invoice in p and "do not add up" in p for p in result["problems"])


def test_underpaid_bill_is_refused_so_unpaid_list_stays_empty(shop):
    with pytest.raises(ValueError):  # credit sales are off by default
        _sell(shop, shirts=2, payments=[{"mode": "cash", "amount_paise": 100000}])
    assert reconcile(shop.conn, TODAY, TODAY, user_id=shop.owner)["unpaid_bills"] == []


def test_series_gap_check(shop):
    for _ in range(3):
        _sell(shop, shirts=1)
    assert series_gap_check(shop.conn, user_id=shop.owner) == []
    shop.conn.execute("DROP TRIGGER sales_no_delete")
    shop.conn.execute("DROP TRIGGER sale_lines_no_delete")
    shop.conn.execute("DELETE FROM sale_lines WHERE sale_id = 2")
    shop.conn.execute("DELETE FROM sales WHERE id = 2")
    gaps = series_gap_check(shop.conn, user_id=shop.owner)
    assert len(gaps) == 1 and gaps[0]["document"] == "bill" and gaps[0]["missing"] == [2]


# ---------- GST register ----------

def test_gst_register_b2b_b2c_and_hsn(shop):
    cust = create_customer(shop.conn, "9000000001", user_id=shop.owner, name="Buyer Ltd", gstin=GSTIN, consent_given=True)
    _sell(shop, shirts=2, blazers=1, customer=cust)                  # B2B, intra-state
    _sell(shop, shirts=1)                                            # B2C 5%
    _sell(shop, blazers=1, intra_state=False)                        # B2C 18%, inter-state: all IGST
    reg = gst_sales_register(shop.conn, TODAY, TODAY, user_id=shop.owner)
    (b2b,) = reg["b2b"]
    assert b2b["customer_gstin"] == GSTIN and b2b["taxable_paise"] == 550000
    assert (b2b["cgst_paise"], b2b["sgst_paise"], b2b["igst_paise"]) == (36500, 36500, 0)
    rates = {r["rate_percent"]: r for r in reg["b2c"]}
    assert rates[5.0]["taxable_paise"] == 100000 and (rates[5.0]["cgst_paise"], rates[5.0]["sgst_paise"]) == (2500, 2500)
    assert rates[18.0]["taxable_paise"] == 350000 and rates[18.0]["igst_paise"] == 63000 and rates[18.0]["cgst_paise"] == 0
    hsn = {(r["hsn"], r["rate_percent"]): r for r in reg["hsn_summary"]}
    assert hsn[("6205", 5.0)]["qty"] == 3 and hsn[("6203", 18.0)]["qty"] == 2
    t = reg["totals"]["sales"]
    assert t["taxable_paise"] == 550000 + 100000 + 350000
    assert t["cgst_paise"] + t["sgst_paise"] + t["igst_paise"] == _summary(shop)["tax_paise"]
    assert "accountant" in reg["note"]


def test_gst_register_credit_notes_and_net(shop):
    cust = create_customer(shop.conn, "9000000002", user_id=shop.owner, gstin=GSTIN, consent_given=True)
    sale = _sell(shop, shirts=2, customer=cust)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id = ?", (sale,)).fetchone()[0]
    process_return(shop.conn, sale, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, reason="x", customer_id=cust)
    reg = gst_sales_register(shop.conn, TODAY, TODAY, user_id=shop.owner)
    (cn,) = reg["credit_notes"]
    assert cn["against_invoice"].startswith("INV/") and cn["customer_gstin"] == GSTIN
    assert (cn["taxable_paise"], cn["cgst_paise"], cn["sgst_paise"]) == (100000, 2500, 2500)
    net = reg["totals"]["net"]
    assert net["taxable_paise"] == 100000 and net["cgst_paise"] == 2500 and net["sgst_paise"] == 2500
    (row,) = reg["hsn_summary"]
    assert (row["qty"], row["returned_qty"], row["net_qty"], row["net_taxable_paise"]) == (2, 1, 1, 100000)


def test_gst_register_keeps_non_gst_bills_apart(shop):
    _sell(shop, shirts=1)
    _sell(shop, shirts=2, mode="unregistered")
    reg = gst_sales_register(shop.conn, TODAY, TODAY, user_id=shop.owner)
    assert reg["non_gst"]["bills"] == 1 and reg["non_gst"]["taxable_paise"] > 0
    assert reg["totals"]["sales"]["taxable_paise"] == 100000  # only the regular bill is in the GST sections


# ---------- permissions and privacy ----------

def test_only_users_with_view_reports_can_read(shop):
    _sell(shop, shirts=1)
    assert _summary(shop, user=shop.ca)["bill_count"] == 1  # accountant can
    for user in (shop.till, shop.store):
        with pytest.raises(PermissionDenied, match="view reports"):
            _summary(shop, user=user)
        with pytest.raises(PermissionDenied):
            day_book(shop.conn, TODAY, TODAY, user_id=user)
        with pytest.raises(PermissionDenied):
            gst_sales_register(shop.conn, TODAY, TODAY, user_id=user)
        with pytest.raises(PermissionDenied):
            series_gap_check(shop.conn, user_id=user)
        with pytest.raises(PermissionDenied):
            reconcile(shop.conn, TODAY, TODAY, user_id=user)


def test_nothing_is_called_profit(shop):
    _sell(shop, shirts=1)
    s = _summary(shop)
    assert not any("profit" in k for k in s) and "profit" not in s["formula"].lower()
    assert not any("profit" in k for k in sales_by(shop.conn, TODAY, TODAY, "product", user_id=shop.owner)[0])
