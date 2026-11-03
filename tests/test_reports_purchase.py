from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import auth
from core.permissions import PermissionDenied, set_user_permission
from core.products import create_product, generate_variants
from core.purchase_returns import process_purchase_return
from core.purchases import (
    cancel_draft_purchase, confirm_purchase, create_draft_purchase, create_supplier,
)
from core.reports_purchase import (
    gst_purchase_summary, price_history, purchase_register, purchase_return_register, purchases_by,
)
from db.connection import open_db

TODAY = date.today()
D_OLD = (TODAY - timedelta(days=40)).isoformat()
D_ONE = (TODAY - timedelta(days=10)).isoformat()
D_TWO = (TODAY - timedelta(days=1)).isoformat()
TODAY_ISO = TODAY.isoformat()
RANGE = ((TODAY - timedelta(days=30)).isoformat(), TODAY_ISO)  # leaves out the old purchase


@pytest.fixture
def shop(tmp_path):
    """Four purchases (one old), a draft, a cancelled draft, and one debit note issued today.

    P0  ABC  A-0   40 days ago   Shirt M Blue 5  @ Rs 450, no GST
    P1  ABC  A-1   10 days ago   Shirt M Blue 10 @ Rs 500, 5% GST, freight Rs 50, Rs 2,000 paid in cash
    P2  XYZ  B-1    1 day ago    Shirt M Blue 10 @ Rs 600 (5%) + Blazer L Black 2 @ Rs 2,000 (18%)
    R1  debit note today on P2: 2 shirts, credited against the bill
    """
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")

    shirt = create_product(conn, "Shirt", hsn="6205", category="Shirts")
    blazer = create_product(conn, "Blazer", hsn="6203", category="Suits")
    (m_blue,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    (l_black,) = generate_variants(conn, blazer, ["L"], ["Black"])
    abc = create_supplier(conn, "ABC Textiles", gstin="27ABCDE1234F1Z5")
    xyz = create_supplier(conn, "XYZ Mills")

    def buy(supplier, invoice_no, day, lines, **kwargs):
        pid = create_draft_purchase(conn, supplier, invoice_no, day, lines, created_by=owner, **kwargs)
        confirm_purchase(conn, pid, user_id=owner, payment_mode="cash")
        return pid

    p0 = buy(abc, "A-0", D_OLD, [{"variant_id": m_blue, "qty": 5, "unit_cost_paise": 45000}])
    p1 = buy(abc, "A-1", D_ONE, [{"variant_id": m_blue, "qty": 10, "unit_cost_paise": 50000, "tax_rate_percent": 5}],
             freight_paise=5000, paid_paise=200000)
    p2 = buy(xyz, "B-1", D_TWO, [
        {"variant_id": m_blue, "qty": 10, "unit_cost_paise": 60000, "tax_rate_percent": 5},
        {"variant_id": l_black, "qty": 2, "unit_cost_paise": 200000, "tax_rate_percent": 18},
    ])
    draft = create_draft_purchase(conn, abc, "A-3", D_TWO, [{"variant_id": m_blue, "qty": 99, "unit_cost_paise": 1}],
                                  created_by=owner)
    dead = create_draft_purchase(conn, xyz, "B-4", D_TWO, [{"variant_id": m_blue, "qty": 99, "unit_cost_paise": 1}],
                                 created_by=owner)
    cancel_draft_purchase(conn, dead)

    shirt_line = conn.execute(
        "SELECT id FROM purchase_lines WHERE purchase_id = ? AND variant_id = ?", (p2, m_blue)
    ).fetchone()[0]
    process_purchase_return(conn, p2, [{"purchase_line_id": shirt_line, "qty": 2}], user_id=owner,
                            reason="defect", settlement="credit")
    yield SimpleNamespace(conn=conn, owner=owner, till=till, ca=ca, abc=abc, xyz=xyz, m_blue=m_blue,
                          l_black=l_black, p0=p0, p1=p1, p2=p2, draft=draft)
    conn.close()


def _by_key(rows):
    return {r["key"]: r for r in rows}


# ---------- permissions and inputs ----------

def test_purchase_reports_need_view_reports_and_view_cost(shop):
    c = shop.conn
    calls = (
        lambda u: purchase_register(c, *RANGE, user_id=u),
        lambda u: purchases_by(c, *RANGE, "supplier", user_id=u),
        lambda u: purchase_return_register(c, *RANGE, user_id=u),
        lambda u: price_history(c, shop.m_blue, user_id=u),
        lambda u: gst_purchase_summary(c, *RANGE, user_id=u),
    )
    for call in calls:
        with pytest.raises(PermissionDenied):
            call(shop.till)
        assert call(shop.ca)                      # accountants have both permissions
    set_user_permission(c, shop.owner, shop.ca, "view_cost", False)
    for call in calls:
        with pytest.raises(PermissionDenied):
            call(shop.ca)


def test_bad_dates_and_group_are_rejected(shop):
    with pytest.raises(ValueError):
        purchase_register(shop.conn, TODAY_ISO, D_OLD, user_id=shop.owner)
    with pytest.raises(ValueError):
        purchases_by(shop.conn, *RANGE, "colour", user_id=shop.owner)
    with pytest.raises(ValueError):
        gst_purchase_summary(shop.conn, "not-a-date", TODAY_ISO, user_id=shop.owner)


# ---------- register ----------

def test_register_lists_only_confirmed_purchases_in_the_period(shop):
    out = purchase_register(shop.conn, *RANGE, user_id=shop.owner)
    assert [r["invoice_no"] for r in out["rows"]] == ["A-1", "B-1"]  # not the old, draft or cancelled ones
    assert out["purchase_count"] == 2
    p1, p2 = out["rows"]
    assert (p1["qty"], p1["subtotal_paise"], p1["tax_paise"], p1["freight_paise"], p1["total_paise"]) == (
        10, 500000, 25000, 5000, 530000)
    assert (p1["paid_paise"], p1["outstanding_paise"], p1["supplier_name"], p1["supplier_gstin"]) == (
        200000, 330000, "ABC Textiles", "27ABCDE1234F1Z5")
    assert (p2["qty"], p2["subtotal_paise"], p2["tax_paise"], p2["total_paise"]) == (12, 1000000, 102000, 1102000)
    assert (p2["returns_paise"], p2["returns_credit_paise"], p2["paid_paise"]) == (126000, 126000, 0)
    assert p2["outstanding_paise"] == 1102000 - 126000
    assert p1["voucher_no"] and p2["voucher_no"] and p1["voucher_no"] != p2["voucher_no"]
    totals = out["totals"]
    assert (totals["qty"], totals["total_paise"], totals["paid_paise"], totals["outstanding_paise"]) == (
        22, 1632000, 200000, 1306000)


def test_register_filters_by_supplier_and_widens_with_the_period(shop):
    only_abc = purchase_register(shop.conn, *RANGE, user_id=shop.owner, supplier_id=shop.abc)
    assert [r["invoice_no"] for r in only_abc["rows"]] == ["A-1"]
    wide = purchase_register(shop.conn, (TODAY - timedelta(days=60)).isoformat(), TODAY_ISO, user_id=shop.owner)
    assert [r["invoice_no"] for r in wide["rows"]] == ["A-0", "A-1", "B-1"]
    assert wide["rows"][0]["total_paise"] == 225000


def test_register_agrees_with_the_supplier_ledger(shop):
    from core.supplier_ledger import supplier_balance
    out = purchase_register(shop.conn, (TODAY - timedelta(days=60)).isoformat(), TODAY_ISO, user_id=shop.owner)
    for supplier in (shop.abc, shop.xyz):
        owed = sum(r["outstanding_paise"] for r in out["rows"] if r["supplier_id"] == supplier)
        assert owed == supplier_balance(shop.conn, supplier, user_id=shop.owner)["balance_paise"]


# ---------- grouped totals ----------

def test_purchases_by_supplier_subtracts_returns(shop):
    out = purchases_by(shop.conn, *RANGE, "supplier", user_id=shop.owner)
    rows = _by_key(out["rows"])
    assert (rows["ABC Textiles"]["qty"], rows["ABC Textiles"]["cost_paise"], rows["ABC Textiles"]["tax_paise"]) == (
        10, 500000, 25000)
    xyz = rows["XYZ Mills"]
    assert (xyz["qty"], xyz["cost_paise"], xyz["tax_paise"]) == (12, 1000000, 102000)
    assert (xyz["returned_qty"], xyz["returned_cost_paise"], xyz["returned_tax_paise"]) == (2, 120000, 6000)
    assert (xyz["net_qty"], xyz["net_cost_paise"], xyz["net_tax_paise"]) == (10, 880000, 96000)
    assert [r["key"] for r in out["rows"]] == ["XYZ Mills", "ABC Textiles"]   # largest first
    assert out["totals"]["net_cost_paise"] == 1380000
    assert "returned to suppliers" in out["formula"]


def test_purchases_by_product_variant_category_and_brand(shop):
    c, owner = shop.conn, shop.owner
    product = _by_key(purchases_by(c, *RANGE, "product", user_id=owner)["rows"])
    assert (product["Shirt"]["qty"], product["Shirt"]["net_cost_paise"]) == (20, 980000)
    assert (product["Blazer"]["qty"], product["Blazer"]["net_cost_paise"]) == (2, 400000)
    variant = _by_key(purchases_by(c, *RANGE, "variant", user_id=owner)["rows"])
    assert variant["Shirt / M / Blue"]["net_qty"] == 18
    assert variant["Blazer / L / Black"]["cost_paise"] == 400000
    category = _by_key(purchases_by(c, *RANGE, "category", user_id=owner)["rows"])
    assert (category["Shirts"]["net_cost_paise"], category["Suits"]["net_cost_paise"]) == (980000, 400000)
    brand = purchases_by(c, *RANGE, "brand", user_id=owner)
    assert [r["key"] for r in brand["rows"]] == ["(none)"]
    assert brand["totals"]["net_cost_paise"] == 1380000


def test_a_return_is_counted_on_the_day_it_was_issued(shop):
    out = purchases_by(shop.conn, *RANGE, "day", user_id=shop.owner)
    assert [r["key"] for r in out["rows"]] == [D_ONE, D_TWO, TODAY_ISO]       # oldest first
    rows = _by_key(out["rows"])
    assert rows[D_ONE]["net_cost_paise"] == 500000
    assert rows[D_TWO]["net_cost_paise"] == 1000000
    assert (rows[TODAY_ISO]["cost_paise"], rows[TODAY_ISO]["returned_cost_paise"], rows[TODAY_ISO]["net_cost_paise"]) == (
        0, 120000, -120000)
    months = purchases_by(shop.conn, *RANGE, "month", user_id=shop.owner)
    assert sum(r["net_cost_paise"] for r in months["rows"]) == 1380000


def test_empty_period_gives_empty_report(shop):
    far = (TODAY - timedelta(days=400)).isoformat(), (TODAY - timedelta(days=300)).isoformat()
    assert purchase_register(shop.conn, *far, user_id=shop.owner)["rows"] == []
    out = purchases_by(shop.conn, *far, "supplier", user_id=shop.owner)
    assert out["rows"] == [] and out["totals"]["net_cost_paise"] == 0


# ---------- debit notes ----------

def test_purchase_return_register(shop):
    out = purchase_return_register(shop.conn, *RANGE, user_id=shop.owner)
    assert out["return_count"] == 1
    r = out["rows"][0]
    assert r["debit_note_no"]
    assert (r["purchase_invoice_no"], r["supplier_name"], r["reason"]) == ("B-1", "XYZ Mills", "defect")
    assert (r["qty"], r["subtotal_paise"], r["tax_paise"], r["total_paise"]) == (2, 120000, 6000, 126000)
    assert (r["due_reduced_paise"], r["refund_paise"]) == (126000, 0)
    assert out["totals"]["total_paise"] == 126000
    assert purchase_return_register(shop.conn, *RANGE, user_id=shop.owner, supplier_id=shop.abc)["rows"] == []
    assert purchase_return_register(shop.conn, *RANGE, user_id=shop.owner, supplier_id=shop.xyz)["return_count"] == 1


# ---------- price history ----------

def test_price_history_shows_every_cost_and_the_change(shop):
    out = price_history(shop.conn, shop.m_blue, user_id=shop.owner)
    assert out["variant"]["product"] == "Shirt"
    assert [(r["invoice_no"], r["unit_cost_paise"], r["change_paise"]) for r in out["rows"]] == [
        ("B-1", 60000, 10000), ("A-1", 50000, 5000), ("A-0", 45000, None),
    ]
    assert (out["lowest_paise"], out["highest_paise"], out["latest_paise"]) == (45000, 60000, 60000)
    only_abc = price_history(shop.conn, shop.m_blue, user_id=shop.owner, supplier_id=shop.abc)
    assert [r["invoice_no"] for r in only_abc["rows"]] == ["A-1", "A-0"]
    assert only_abc["latest_paise"] == 50000


def test_price_history_ignores_drafts_and_unknown_variants(shop):
    out = price_history(shop.conn, shop.m_blue, user_id=shop.owner)
    assert 1 not in [r["unit_cost_paise"] for r in out["rows"]]        # the Re 1 draft is not a price
    assert price_history(shop.conn, shop.l_black, user_id=shop.owner)["latest_paise"] == 200000
    with pytest.raises(ValueError):
        price_history(shop.conn, 9999, user_id=shop.owner)


# ---------- GST on purchases ----------

def test_gst_purchase_summary_by_rate_is_net_of_debit_notes(shop):
    out = gst_purchase_summary(shop.conn, *RANGE, user_id=shop.owner)
    assert [r["rate_percent"] for r in out["by_rate"]] == [5.0, 18.0]
    five, eighteen = out["by_rate"]
    assert (five["purchases_taxable_paise"], five["purchases_tax_paise"], five["invoice_count"]) == (1100000, 55000, 2)
    assert (five["returns_taxable_paise"], five["returns_tax_paise"]) == (120000, 6000)
    assert (five["net_taxable_paise"], five["net_tax_paise"]) == (980000, 49000)
    assert (eighteen["purchases_taxable_paise"], eighteen["purchases_tax_paise"]) == (400000, 72000)
    assert (eighteen["returns_tax_paise"], eighteen["net_tax_paise"]) == (0, 72000)
    totals = out["totals"]
    assert (totals["purchases_tax_paise"], totals["returns_tax_paise"], totals["net_tax_paise"]) == (127000, 6000, 121000)
    assert totals["net_taxable_paise"] == 1380000
    assert "accountant" in out["note"]


def test_gst_purchase_summary_by_supplier(shop):
    out = gst_purchase_summary(shop.conn, *RANGE, user_id=shop.owner)
    rows = {r["supplier_name"]: r for r in out["by_supplier"]}
    abc, xyz = rows["ABC Textiles"], rows["XYZ Mills"]
    assert (abc["supplier_gstin"], abc["invoice_count"], abc["net_taxable_paise"], abc["net_tax_paise"]) == (
        "27ABCDE1234F1Z5", 1, 500000, 25000)
    assert (xyz["supplier_gstin"], xyz["invoice_count"]) == (None, 1)
    assert (xyz["purchases_tax_paise"], xyz["returns_tax_paise"], xyz["net_taxable_paise"], xyz["net_tax_paise"]) == (
        102000, 6000, 880000, 96000)
    assert sum(r["net_tax_paise"] for r in out["by_supplier"]) == out["totals"]["net_tax_paise"]
