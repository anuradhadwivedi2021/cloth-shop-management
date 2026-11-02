from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import auth
from core.permissions import PermissionDenied, set_user_permission
from core.products import create_product, generate_variants
from core.purchase_returns import process_purchase_return
from core.purchases import confirm_purchase, create_draft_purchase, create_supplier
from core.reports_stock import (
    current_stock, damaged_stock, low_stock, movement_ledger, reorder_list, stock_flow, stock_grid,
    stock_valuation,
)
from core.sales import create_sale
from core.stock import add_movement
from core.stock_ops import mark_damaged
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

TODAY = date.today()
TODAY_ISO = TODAY.isoformat()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
CASH = [{"mode": "cash", "amount_paise": 10 ** 7}]


@pytest.fixture
def shop(tmp_path):
    """A shirt (S/M/L x Blue/Black, reorder level 5), a blazer, a tie, with real purchases, a sale,
    a purchase return and a damaged item, all dated today."""
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    ensure_default_tax_rules(conn)

    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000, category="Shirts",
                           brand="Raymond", reorder_level=5)
    blazer = create_product(conn, "Blazer", hsn="6203", default_price_paise=413000, category="Suits",
                            brand="Peter", default_cost_paise=200000)
    tie = create_product(conn, "Tie", hsn="6217", default_price_paise=20000, category="Accessories")
    ids = generate_variants(conn, shirt, ["S", "M", "L"], ["Blue", "Black"])
    shirt_v = dict(zip([(s, c) for s in ("S", "M", "L") for c in ("Blue", "Black")], ids))
    (blazer_l,) = generate_variants(conn, blazer, ["L"], ["Black"])
    (tie_free,) = generate_variants(conn, tie, ["Free"], ["Red"])

    abc = create_supplier(conn, "ABC Textiles")
    xyz = create_supplier(conn, "XYZ Mills")
    m_blue, l_blue = shirt_v[("M", "Blue")], shirt_v[("L", "Blue")]
    p1 = create_draft_purchase(conn, abc, "A-1", TODAY_ISO, [
        {"variant_id": m_blue, "qty": 10, "unit_cost_paise": 50000},
        {"variant_id": l_blue, "qty": 4, "unit_cost_paise": 50000},
    ], created_by=owner)
    confirm_purchase(conn, p1, user_id=owner)
    p2 = create_draft_purchase(conn, xyz, "X-1", TODAY_ISO, [
        {"variant_id": m_blue, "qty": 10, "unit_cost_paise": 60000},
    ], created_by=owner)
    confirm_purchase(conn, p2, user_id=owner)
    with transaction(conn):
        add_movement(conn, blazer_l, 10, "opening stock", user_id=owner)
        add_movement(conn, tie_free, 5, "opening stock", user_id=owner)

    create_sale(conn, [{"variant_id": m_blue, "qty": 12}], CASH, user_id=till)
    line_id = conn.execute("SELECT id FROM purchase_lines WHERE purchase_id = ?", (p2,)).fetchone()[0]
    process_purchase_return(conn, p2, [{"purchase_line_id": line_id, "qty": 2}], user_id=owner, reason="defect")
    mark_damaged(conn, l_blue, 1, "torn", user_id=store)
    with transaction(conn):
        add_movement(conn, blazer_l, 2, "customer return", bucket="returned_pending",
                     doc_type="sale_return", user_id=owner)

    yield SimpleNamespace(
        conn=conn, owner=owner, till=till, ca=ca, store=store, shirt=shirt, blazer=blazer_l, tie=tie_free,
        v=shirt_v, m_blue=m_blue, l_blue=l_blue, abc=abc,
    )
    conn.close()


def _by_id(rows):
    return {r["variant_id"]: r for r in rows}


# ---------- permissions ----------

def test_stock_reports_need_the_view_reports_permission(shop):
    c = shop.conn
    for call in (
        lambda: current_stock(c, user_id=shop.till),
        lambda: stock_grid(c, shop.shirt, user_id=shop.till),
        lambda: low_stock(c, user_id=shop.till),
        lambda: reorder_list(c, user_id=shop.till),
        lambda: movement_ledger(c, shop.m_blue, user_id=shop.till),
        lambda: stock_flow(c, TODAY_ISO, TODAY_ISO, user_id=shop.till),
        lambda: damaged_stock(c, user_id=shop.till),
        lambda: stock_valuation(c, user_id=shop.till),
    ):
        with pytest.raises(PermissionDenied):
            call()
    assert current_stock(c, user_id=shop.ca)  # accountants may view


# ---------- stock by variant ----------

def test_current_stock_comes_from_movements_and_keeps_buckets_apart(shop):
    rows = _by_id(current_stock(shop.conn, user_id=shop.owner))
    assert rows[shop.m_blue]["sellable_qty"] == 6          # 10 + 10 - 12 sold - 2 returned to supplier
    assert rows[shop.l_blue]["sellable_qty"] == 3          # 4 - 1 damaged
    assert rows[shop.l_blue]["damaged_qty"] == 1
    assert rows[shop.blazer]["sellable_qty"] == 10
    assert rows[shop.blazer]["returned_pending_qty"] == 2  # not billable until checked
    assert rows[shop.v[("S", "Blue")]]["sellable_qty"] == 0


def test_stock_report_always_equals_the_movement_ledger(shop):
    for row in current_stock(shop.conn, user_id=shop.owner, include_inactive=True):
        ledger = movement_ledger(shop.conn, row["variant_id"], user_id=shop.owner)
        assert ledger["closing"]["sellable"] == row["sellable_qty"]
        assert ledger["closing"]["damaged"] == row["damaged_qty"]
        assert ledger["closing"]["returned_pending"] == row["returned_pending_qty"]


def test_status_uses_the_variant_level_if_set_otherwise_the_product_level(shop):
    rows = _by_id(current_stock(shop.conn, user_id=shop.owner))
    assert rows[shop.m_blue]["status"] == "ok"              # 6 >= 5
    assert rows[shop.l_blue]["status"] == "low"             # 3 < 5
    assert rows[shop.v[("S", "Blue")]]["status"] == "out"
    assert rows[shop.blazer]["status"] == "ok"              # no reorder level set
    shop.conn.execute("UPDATE variants SET reorder_level = 2 WHERE id = ?", (shop.l_blue,))
    assert _by_id(current_stock(shop.conn, user_id=shop.owner))[shop.l_blue]["status"] == "ok"


def test_filters(shop):
    c, owner = shop.conn, shop.owner
    assert {r["product"] for r in current_stock(c, user_id=owner, category="shirts")} == {"Shirt"}
    assert {r["product"] for r in current_stock(c, user_id=owner, brand="PETER")} == {"Blazer"}
    assert {r["colour"] for r in current_stock(c, user_id=owner, product_id=shop.shirt, search="blue")} == {"Blue"}
    assert [r["variant_id"] for r in current_stock(c, user_id=owner, status="low")] == [shop.l_blue]
    assert len(current_stock(c, user_id=owner, product_id=shop.shirt, status="out")) == 4
    with pytest.raises(ValueError):
        current_stock(c, user_id=owner, status="nonsense")


def test_inactive_variants_are_hidden_unless_asked_for(shop):
    victim = shop.v[("S", "Black")]
    shop.conn.execute("UPDATE variants SET is_active = 0 WHERE id = ?", (victim,))
    assert victim not in _by_id(current_stock(shop.conn, user_id=shop.owner))
    assert victim in _by_id(current_stock(shop.conn, user_id=shop.owner, include_inactive=True))


# ---------- grid ----------

def test_size_by_colour_grid(shop):
    grid = stock_grid(shop.conn, shop.shirt, user_id=shop.owner)
    assert grid["sizes"] == ["S", "M", "L"]
    assert grid["colours"] == ["Black", "Blue"]
    rows = {r["colour"]: r for r in grid["rows"]}
    assert rows["Blue"]["quantities"] == [0, 6, 3]
    assert rows["Blue"]["total"] == 9
    assert rows["Black"]["quantities"] == [0, 0, 0]
    assert grid["size_totals"] == [0, 6, 3]
    assert grid["total"] == 9
    with pytest.raises(ValueError):
        stock_grid(shop.conn, 9999, user_id=shop.owner)


def test_grid_marks_missing_combinations_and_sorts_sizes_naturally(shop):
    c = shop.conn
    kurti = create_product(c, "Kurti")
    generate_variants(c, kurti, ["S"], ["Red"])
    generate_variants(c, kurti, ["M"], ["Blue"])
    grid = stock_grid(c, kurti, user_id=shop.owner)
    rows = {r["colour"]: r["quantities"] for r in grid["rows"]}
    assert rows == {"Blue": [None, 0], "Red": [0, None]}

    mixed = create_product(c, "Mixed")
    generate_variants(c, mixed, ["XL", "30", "S", "XXL", "28", "M"], ["Grey"])
    assert stock_grid(c, mixed, user_id=shop.owner)["sizes"] == ["S", "M", "XL", "XXL", "28", "30"]


# ---------- low stock and reorder ----------

def test_low_stock_lists_variants_below_their_reorder_level(shop):
    rows = low_stock(shop.conn, user_id=shop.owner)
    names = {(r["size"], r["colour"]) for r in rows}
    assert names == {("S", "Blue"), ("S", "Black"), ("M", "Black"), ("L", "Black"), ("L", "Blue")}
    low = {r["variant_id"]: r for r in rows}[shop.l_blue]
    assert (low["sellable_qty"], low["reorder_level"], low["shortfall"]) == (3, 5, 2)
    assert shop.blazer not in {r["variant_id"] for r in rows}  # reorder level not set


def test_reorder_list_is_grouped_by_the_last_supplier_and_never_orders(shop):
    out = reorder_list(shop.conn, user_id=shop.owner)
    assert "automatically" in out["note"]
    assert out["item_count"] == 5
    first, last = out["groups"][0], out["groups"][-1]
    assert (first["supplier_name"], [i["variant_id"] for i in first["items"]]) == ("ABC Textiles", [shop.l_blue])
    item = first["items"][0]
    assert (item["current_qty"], item["suggested_qty"], item["last_cost_paise"]) == (3, 7, 50000)  # 5 x 2 - 3
    assert last["supplier_id"] is None and last["supplier_name"] == "No supplier recorded"
    assert all(i["suggested_qty"] == 10 and i["last_cost_paise"] is None for i in last["items"])
    assert reorder_list(shop.conn, user_id=shop.owner, target_multiple=3)["groups"][0]["items"][0]["suggested_qty"] == 12
    with pytest.raises(ValueError):
        reorder_list(shop.conn, user_id=shop.owner, target_multiple=0)


def test_purchase_cost_is_hidden_from_users_without_view_cost(shop):
    set_user_permission(shop.conn, shop.owner, shop.ca, "view_cost", False)
    out = reorder_list(shop.conn, user_id=shop.ca)
    assert out["groups"][0]["items"][0]["last_cost_paise"] is None
    with pytest.raises(PermissionDenied):
        stock_valuation(shop.conn, user_id=shop.ca)


# ---------- ledger and flow ----------

def test_movement_ledger_lists_every_movement_with_running_balance(shop):
    ledger = movement_ledger(shop.conn, shop.m_blue, user_id=shop.owner)
    assert [(r["doc_type"], r["qty_change"], r["balance_after"]) for r in ledger["rows"]] == [
        ("purchase", 10, 10), ("purchase", 10, 20), ("sale", -12, 8), ("purchase_return", -2, 6),
    ]
    assert ledger["opening"]["sellable"] == 0 and ledger["closing"]["sellable"] == 6
    assert ledger["rows"][0]["user_name"] == "Anuradha"
    assert ledger["variant"]["product"] == "Shirt"


def test_movement_ledger_date_range_carries_the_opening_balance(shop):
    later = movement_ledger(shop.conn, shop.m_blue, user_id=shop.owner, date_from=TOMORROW)
    assert later["rows"] == []
    assert (later["opening"]["sellable"], later["closing"]["sellable"]) == (6, 6)
    with pytest.raises(ValueError):
        movement_ledger(shop.conn, 9999, user_id=shop.owner)
    with pytest.raises(ValueError):
        movement_ledger(shop.conn, shop.m_blue, user_id=shop.owner, date_from=TOMORROW, date_to=TODAY_ISO)


def test_stock_flow_explains_every_change(shop):
    flow = stock_flow(shop.conn, TODAY_ISO, TODAY_ISO, user_id=shop.owner)
    rows = _by_id(flow["rows"])
    m = rows[shop.m_blue]
    assert (m["opening"], m["received"], m["purchase_returned"], m["sold"], m["closing"]) == (0, 20, 2, 12, 6)
    low = rows[shop.l_blue]
    assert (low["received"], low["adjustments"], low["closing"]) == (4, -1, 3)   # damaged leaves sellable stock
    assert rows[shop.blazer]["other"] == 10                                      # opening stock entry
    for r in flow["rows"]:
        assert r["opening"] + r["received"] - r["purchase_returned"] - r["sold"] + r["adjustments"] \
            + r["count_differences"] + r["other"] == r["closing"]
    assert flow["totals"]["sold"] == 12
    assert "closing" in flow["formula"]


def test_stock_flow_for_other_periods(shop):
    before = stock_flow(shop.conn, (TODAY - timedelta(days=5)).isoformat(),
                        (TODAY - timedelta(days=1)).isoformat(), user_id=shop.owner)
    assert before["rows"] == []
    after = _by_id(stock_flow(shop.conn, TOMORROW, TOMORROW, user_id=shop.owner)["rows"])
    assert (after[shop.m_blue]["opening"], after[shop.m_blue]["sold"], after[shop.m_blue]["closing"]) == (6, 0, 6)
    only = stock_flow(shop.conn, TODAY_ISO, TODAY_ISO, user_id=shop.owner, product_id=shop.shirt)
    assert {r["product"] for r in only["rows"]} == {"Shirt"}
    with pytest.raises(ValueError):
        stock_flow(shop.conn, TOMORROW, TODAY_ISO, user_id=shop.owner)


# ---------- unsellable stock ----------

def test_damaged_and_unsellable_stock_is_listed_separately(shop):
    out = damaged_stock(shop.conn, user_id=shop.owner)
    assert out["damaged_units"] == 1
    assert out["returned_pending_units"] == 2
    assert {r["variant_id"] for r in out["rows"]} == {shop.l_blue, shop.blazer}


# ---------- valuation ----------

def test_weighted_average_valuation_is_net_of_purchase_returns(shop):
    out = stock_valuation(shop.conn, user_id=shop.owner)
    assert out["method"] == "weighted_average"
    assert "Weighted average" in out["method_label"]
    rows = _by_id(out["rows"])
    # M blue: bought 10 @500 + 10 @600, returned 2 @600 -> (11000 - 1200) / 18 per piece; 6 pieces on hand
    assert (rows[shop.m_blue]["unit_cost_paise"], rows[shop.m_blue]["value_paise"]) == (54444, 326667)
    assert (rows[shop.l_blue]["unit_cost_paise"], rows[shop.l_blue]["value_paise"]) == (50000, 150000)
    assert rows[shop.blazer]["cost_source"] == "product_default"
    assert rows[shop.blazer]["value_paise"] == 2000000
    assert rows[shop.tie]["cost_source"] == "missing" and rows[shop.tie]["value_paise"] == 0
    assert out["variants_without_cost"] == 1
    assert out["total_qty"] == 6 + 3 + 10 + 5
    assert out["total_value_paise"] == 326667 + 150000 + 2000000
    assert shop.v[("S", "Blue")] not in rows  # nothing on hand, nothing to value


def test_last_purchase_cost_valuation(shop):
    out = stock_valuation(shop.conn, user_id=shop.owner, method="last_purchase_cost")
    rows = _by_id(out["rows"])
    assert (rows[shop.m_blue]["unit_cost_paise"], rows[shop.m_blue]["value_paise"]) == (60000, 360000)
    assert out["total_value_paise"] == 360000 + 150000 + 2000000
    assert out["method_label"] == "Last purchase cost"


def test_damaged_stock_is_not_valued_and_filters_apply(shop):
    out = stock_valuation(shop.conn, user_id=shop.owner, category="Shirts")
    assert out["total_qty"] == 9  # the 1 damaged shirt is not sellable stock
    assert {r["product"] for r in out["rows"]} == {"Shirt"}
    with pytest.raises(ValueError):
        stock_valuation(shop.conn, user_id=shop.owner, method="fifo")
