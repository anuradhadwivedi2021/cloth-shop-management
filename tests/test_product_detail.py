"""Product detail screen backend: detail data, adding sizes/colours, saving only what changed."""

import pytest

from core import auth
from core.audit import verify_chain
from core.permissions import PermissionDenied, set_user_permission
from core.money import parse_rupees
from core.product_detail import add_variants, get_product_detail, preview_new_variants, rupees_text, save_changes
from core.product_edit import add_product, set_product_active, set_rack_location, set_variant_active
from core.stock import add_movement
from db.connection import open_db, transaction


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")      # view_products only
    store = auth.create_user(conn, owner, "store", "store-password", "stock")     # edit_products, no change_price
    ca = auth.create_user(conn, owner, "ca", "ca-password", "accountant")         # view_cost
    made = add_product(conn, owner, "Shirt", sizes=["S", "M"], colours=["Blue"], hsn="6205", category="Shirts",
                       default_price_paise=105000, default_cost_paise=60000, mrp_paise=120000)
    first, second = made["variant_ids"]
    with transaction(conn):
        add_movement(conn, first, 7, "opening stock")
        add_movement(conn, second, 3, "opening stock")
    yield type("S", (), {"conn": conn, "owner": owner, "till": till, "store": store, "ca": ca,
                         "pid": made["product_id"], "v1": first, "v2": second})
    conn.close()


def _count(s, table="variants"):
    return s.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- detail

def test_detail_has_product_variants_stock_and_effective_price(shop):
    s = shop
    d = get_product_detail(s.conn, s.owner, s.pid)
    assert d["product"]["name"] == "Shirt" and d["product"]["price_paise"] == 105000
    assert d["product"]["cost_paise"] == 60000 and d["product"]["mrp_paise"] == 120000
    assert [(v["size"], v["sellable_qty"]) for v in d["variants"]] == [("S", 7), ("M", 3)]
    assert d["totals"] == {"variants": 2, "active_variants": 2, "sellable_qty": 10}
    assert all(v["price_paise"] == 105000 and v["barcode"] for v in d["variants"])
    assert d["can"] == {"edit": True, "change_price": True, "view_cost": True}
    assert all(isinstance(v["price_paise"], int) for v in d["variants"])


def test_cost_is_hidden_without_view_cost_and_permissions_are_reported(shop):
    s = shop
    d = get_product_detail(s.conn, s.till, s.pid)
    assert d["product"]["cost_paise"] is None
    assert d["can"] == {"edit": False, "change_price": False, "view_cost": False}
    assert get_product_detail(s.conn, s.store, s.pid)["can"]["edit"] is True


def test_detail_needs_view_products_and_a_real_product(shop):
    s = shop
    set_user_permission(s.conn, s.owner, s.till, "view_products", False)
    with pytest.raises(PermissionDenied):
        get_product_detail(s.conn, s.till, s.pid)
    with pytest.raises(ValueError, match="not found"):
        get_product_detail(s.conn, s.owner, 999)


def test_variant_price_override_shows_as_the_price(shop):
    s = shop
    from core.product_edit import set_variant_price
    set_variant_price(s.conn, s.owner, s.v2, 99000)
    prices = {v["id"]: v["price_paise"] for v in get_product_detail(s.conn, s.owner, s.pid)["variants"]}
    assert prices == {s.v1: 105000, s.v2: 99000}


# ---------------------------------------------------------------- add sizes / colours

def test_preview_splits_new_from_existing_ignoring_case(shop):
    s = shop
    p = preview_new_variants(s.conn, s.store, s.pid, "m, L", "blue, Red")
    assert p["existing"] == [("m", "blue")]
    assert p["new"] == [("m", "Red"), ("L", "blue"), ("L", "Red")]
    assert _count(s) == 2                                               # a preview writes nothing
    with pytest.raises(ValueError, match="at least one size"):
        preview_new_variants(s.conn, s.store, s.pid, "", "Red")


def test_add_variants_creates_only_the_new_ones_with_codes_and_audit(shop):
    s = shop
    out = add_variants(s.conn, s.store, s.pid, "M, L", "Blue, Red")     # M-Blue exists
    assert out["skipped"] == 1 and len(out["variant_ids"]) == 3 and _count(s) == 5
    d = get_product_detail(s.conn, s.owner, s.pid)
    assert [(v["size"], v["colour"]) for v in d["variants"]][2:] == [("M", "Red"), ("L", "Blue"), ("L", "Red")]
    assert len({v["sku"] for v in d["variants"]}) == 5 and len({v["barcode"] for v in d["variants"]}) == 5
    assert all(v["sellable_qty"] == 0 for v in d["variants"][2:])
    entry = s.conn.execute("SELECT * FROM audit_log WHERE action = 'variants_added'").fetchone()
    assert entry["entity_id"] == s.pid and entry["user_id"] == s.store
    assert verify_chain(s.conn) == (True, None)


def test_adding_what_already_exists_is_refused_and_saves_nothing(shop):
    s = shop
    before = s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    with pytest.raises(ValueError, match="already exist"):
        add_variants(s.conn, s.owner, s.pid, "s, M", "blue")
    assert _count(s) == 2 and s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


def test_add_variants_needs_edit_products_in_core(shop):
    s = shop
    for call in (add_variants, preview_new_variants):
        with pytest.raises(PermissionDenied, match="edit products"):
            call(s.conn, s.till, s.pid, "XL", "Blue")
    with pytest.raises(ValueError, match="not found"):
        add_variants(s.conn, s.owner, 999, "XL", "Blue")
    assert _count(s) == 2


# ---------------------------------------------------------------- save only what changed

def test_save_changes_sends_only_real_changes_and_reports_them(shop):
    s = shop
    out = save_changes(s.conn, s.owner, s.pid,
                       fields={"name": "Shirt", "category": "Formal shirts", "brand": "", "hsn": "6205",
                               "material": "", "season": "Summer", "description": ""},
                       prices={"price": "1,100", "cost": "600", "mrp": "1200"},
                       gst_text="5", reorder_text="4", unit="piece")
    assert out == {"details": ["category", "gst_rate_percent", "reorder_level", "season"],
                   "prices": ["price_paise"], "error": None}
    d = get_product_detail(s.conn, s.owner, s.pid)["product"]
    assert (d["category"], d["season"], d["price_paise"], d["reorder_level"], d["gst_rate_percent"]) == \
           ("Formal shirts", "Summer", 110000, 4, 5.0)
    assert save_changes(s.conn, s.owner, s.pid, fields={"name": "Shirt"}, prices={"price": "1100"}) == \
           {"details": [], "prices": [], "error": None}


def test_mrp_can_be_cleared_and_price_above_mrp_is_refused(shop):
    s = shop
    save_changes(s.conn, s.owner, s.pid, prices={"mrp": ""})
    assert get_product_detail(s.conn, s.owner, s.pid)["product"]["mrp_paise"] is None
    save_changes(s.conn, s.owner, s.pid, prices={"mrp": "1200"})
    with pytest.raises(ValueError, match="above the MRP"):
        save_changes(s.conn, s.owner, s.pid, fields={"name": "Shirt"}, prices={"price": "1300"})
    assert get_product_detail(s.conn, s.owner, s.pid)["product"]["price_paise"] == 105000


def test_bad_text_saves_nothing_at_all(shop):
    s = shop
    before = get_product_detail(s.conn, s.owner, s.pid)["product"]
    audit_before = s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    for kwargs, message in (
        ({"fields": {"name": "  "}}, "name is required"),
        ({"fields": {"category": "X"}, "prices": {"price": "12.345"}}, "Selling price: .*2 decimal"),
        ({"fields": {"category": "X"}, "prices": {"price": "abc"}}, "Selling price"),
        ({"fields": {"category": "X"}, "gst_text": "abc"}, "GST rate"),
        ({"fields": {"category": "X"}, "gst_text": "150"}, "between 0 and 100"),
        ({"fields": {"category": "X"}, "reorder_text": "-2"}, "whole number"),
        ({"fields": {"category": "X"}, "unit": "box"}, "Unit must be"),
    ):
        with pytest.raises(ValueError, match=message):
            save_changes(s.conn, s.owner, s.pid, **kwargs)
    assert get_product_detail(s.conn, s.owner, s.pid)["product"] == before
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audit_before


def test_details_saved_but_prices_refused_is_reported_honestly(shop):
    s = shop                                                            # store: edit_products yes, change_price no
    out = save_changes(s.conn, s.store, s.pid, fields={"name": "Shirt", "category": "Kurta"}, prices={"price": "999"})
    assert out["details"] == ["category"] and out["prices"] == [] and "not allowed to change prices" in out["error"]
    d = get_product_detail(s.conn, s.owner, s.pid)["product"]
    assert d["category"] == "Kurta" and d["price_paise"] == 105000
    with pytest.raises(PermissionDenied):                               # only a price change: nothing saved, raises
        save_changes(s.conn, s.store, s.pid, prices={"price": "998"})
    with pytest.raises(PermissionDenied):                               # no edit permission at all
        save_changes(s.conn, s.till, s.pid, fields={"category": "Z"})


def test_unchanged_prices_do_not_need_the_price_permission(shop):
    s = shop
    out = save_changes(s.conn, s.store, s.pid, fields={"season": "Winter"}, prices={"price": "1050", "mrp": "1200"})
    assert out == {"details": ["season"], "prices": [], "error": None}


# ---------------------------------------------------------------- what the screen also calls

def test_rack_for_many_and_active_warning_come_from_core(shop):
    s = shop
    assert set_rack_location(s.conn, s.store, [s.v1, s.v2], "A-3") == 2
    d = get_product_detail(s.conn, s.owner, s.pid)
    assert {v["rack_location"] for v in d["variants"]} == {"A-3"}
    assert set_variant_active(s.conn, s.owner, s.v2, False) == {"changed": True, "sellable_stock_left": 3}
    d = get_product_detail(s.conn, s.owner, s.pid)
    assert d["totals"] == {"variants": 2, "active_variants": 1, "sellable_qty": 10}
    assert set_product_active(s.conn, s.owner, s.pid, False) == {"changed": True, "sellable_stock_left": 10}
    assert get_product_detail(s.conn, s.owner, s.pid)["product"]["is_active"] is False
    assert set_product_active(s.conn, s.owner, s.pid, True)["changed"] is True


def test_rupees_text_round_trips_through_parse_rupees():
    assert rupees_text(None) == "" and rupees_text(0) == "0.00" and rupees_text(105050) == "1050.50"
    assert rupees_text(5) == "0.05" and rupees_text(123456789) == "1234567.89"
    for paise in (0, 5, 100, 105050, 99999999):
        assert parse_rupees(rupees_text(paise)) == paise