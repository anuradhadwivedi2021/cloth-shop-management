"""Items that did not sell (PDF 1 section 16.5: slow-moving items and items with no sales in a period)."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import auth
from core.permissions import PermissionDenied, set_user_permission
from core.products import create_product, generate_variants
from core.reports_stock import unsold_stock
from core.sales import create_sale
from core.stock import add_movement
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

TODAY = date.today()
CASH = [{"mode": "cash", "amount_paise": 10 ** 7}]


@pytest.fixture
def shop(tmp_path):
    """Shirt S (3, never sold), Shirt M (10, 4 sold today), Shirt L (0 in stock), Blazer (5, sold 1 today),
    Tie (5, never sold, other category and brand)."""
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000, category="Shirts", brand="Raymond")
    blazer = create_product(conn, "Blazer", hsn="6203", default_price_paise=413000, category="Suits", brand="Peter")
    tie = create_product(conn, "Tie", hsn="6217", default_price_paise=20000, category="Accessories", brand="Zodiac")
    s_blue, m_blue, l_blue = generate_variants(conn, shirt, ["S", "M", "L"], ["Blue"])
    (blazer_l,) = generate_variants(conn, blazer, ["L"], ["Black"])
    (tie_free,) = generate_variants(conn, tie, ["Free"], ["Red"])
    with transaction(conn):
        add_movement(conn, s_blue, 3, "opening stock", user_id=owner)
        add_movement(conn, m_blue, 10, "opening stock", user_id=owner)
        add_movement(conn, blazer_l, 5, "opening stock", user_id=owner)
        add_movement(conn, tie_free, 5, "opening stock", user_id=owner)
    create_sale(conn, [{"variant_id": m_blue, "qty": 4}], CASH, user_id=till)
    create_sale(conn, [{"variant_id": blazer_l, "qty": 1}], CASH, user_id=till)
    yield SimpleNamespace(conn=conn, owner=owner, till=till, ca=ca, s=s_blue, m=m_blue, l=l_blue, blazer=blazer_l,
                          tie=tie_free)
    conn.close()


def _names(report):
    return [(r["product"], r["size"]) for r in report["rows"]]


def test_items_that_did_not_sell_at_all_in_the_period(shop):
    report = unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner)
    # the shirt S and the tie never sold; shirt M and the blazer sold today; shirt L has no stock
    assert sorted(_names(report)) == [("Shirt", "S"), ("Tie", "Free")]
    assert report["variants"] == 2 and report["units_in_stock"] == 8 and report["max_sold"] == 0
    assert all(r["pieces_sold"] == 0 and r["last_sold"] is None and r["days_since_last_sale"] is None
               for r in report["rows"])


def test_slow_movers_are_items_that_sold_few_pieces(shop):
    report = unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner, max_sold=1)
    assert sorted(_names(report)) == [("Blazer", "L"), ("Shirt", "S"), ("Tie", "Free")]  # the blazer sold exactly 1
    blazer = next(r for r in report["rows"] if r["product"] == "Blazer")
    assert blazer["pieces_sold"] == 1 and blazer["in_stock"] == 4 and blazer["last_sold"] == TODAY.isoformat()
    assert blazer["days_since_last_sale"] == 0
    assert ("Shirt", "M") not in _names(report)  # 4 pieces sold: not slow
    assert ("Shirt", "M") in _names(unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner, max_sold=4))


def test_a_period_without_sales_lists_everything_with_stock_and_counts_the_days(shop):
    end = TODAY + timedelta(days=10)
    report = unsold_stock(shop.conn, TODAY + timedelta(days=1), end, user_id=shop.owner)
    assert sorted(_names(report)) == [("Blazer", "L"), ("Shirt", "M"), ("Shirt", "S"), ("Tie", "Free")]
    shirt_m = next(r for r in report["rows"] if (r["product"], r["size"]) == ("Shirt", "M"))
    assert shirt_m["pieces_sold"] == 0 and shirt_m["last_sold"] == TODAY.isoformat()
    assert shirt_m["days_since_last_sale"] == 10 and shirt_m["in_stock"] == 6


def test_never_sold_comes_first_then_the_oldest_sale(shop):
    report = unsold_stock(shop.conn, TODAY + timedelta(days=1), TODAY + timedelta(days=10), user_id=shop.owner)
    never = [r for r in report["rows"] if r["days_since_last_sale"] is None]
    assert len(never) == 2 and report["rows"][:2] == never
    assert [r["days_since_last_sale"] for r in report["rows"][2:]] == [10, 10]


def test_stock_you_cannot_sell_is_not_listed(shop):
    names = _names(unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner, max_sold=99))
    assert ("Shirt", "L") not in names  # nothing in stock
    shop.conn.execute("UPDATE variants SET is_active = 0 WHERE id = ?", (shop.tie,))
    names = _names(unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner, max_sold=99))
    assert ("Tie", "Free") not in names  # hidden items are not listed


def test_filters_by_category_and_brand(shop):
    only_shirts = unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner, category="shirts")
    assert _names(only_shirts) == [("Shirt", "S")]
    only_zodiac = unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner, brand="Zodiac")
    assert _names(only_zodiac) == [("Tie", "Free")]


def test_stock_value_is_shown_only_to_those_who_may_see_cost(shop):
    from core.purchases import confirm_purchase, create_draft_purchase, create_supplier
    abc = create_supplier(shop.conn, "ABC Textiles")
    purchase = create_draft_purchase(shop.conn, abc, "A-1", TODAY.isoformat(), [
        {"variant_id": shop.s, "qty": 2, "unit_cost_paise": 50000}], created_by=shop.owner)
    confirm_purchase(shop.conn, purchase, user_id=shop.owner)  # now 5 of shirt S, last cost Rs 500
    report = unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner)
    shirt_s = next(r for r in report["rows"] if r["product"] == "Shirt")
    tie = next(r for r in report["rows"] if r["product"] == "Tie")
    assert shirt_s["in_stock"] == 5 and shirt_s["stock_value_paise"] == 250000
    assert tie["stock_value_paise"] is None  # opening stock has no purchase cost on record
    assert report["stock_value_paise"] == 250000
    set_user_permission(shop.conn, shop.owner, shop.ca, "view_cost", False)
    hidden = unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.ca)
    assert "stock_value_paise" not in hidden and all("stock_value_paise" not in r for r in hidden["rows"])
    assert hidden["variants"] == 2


def test_it_needs_the_view_reports_permission(shop):
    with pytest.raises(PermissionDenied):
        unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.till)


@pytest.mark.parametrize("kwargs, message", [
    (dict(max_sold=-1), "zero or more"),
    (dict(max_sold=True), "zero or more"),
    (dict(max_sold="2"), "zero or more"),
    (dict(max_sold=1.5), "zero or more"),
])
def test_wrong_piece_counts_are_refused(shop, kwargs, message):
    with pytest.raises(ValueError, match=message):
        unsold_stock(shop.conn, TODAY, TODAY, user_id=shop.owner, **kwargs)


def test_wrong_dates_are_refused(shop):
    with pytest.raises(ValueError, match="cannot be after"):
        unsold_stock(shop.conn, TODAY, TODAY - timedelta(days=1), user_id=shop.owner)
    with pytest.raises(ValueError, match="valid date"):
        unsold_stock(shop.conn, "31/12/2026", TODAY, user_id=shop.owner)