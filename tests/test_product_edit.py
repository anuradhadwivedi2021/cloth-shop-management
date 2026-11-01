from types import SimpleNamespace

import pytest

from core import product_edit as pe
from core.audit import verify_chain
from core.permissions import PermissionDenied, set_user_permission
from core.products import create_product, generate_variants
from core.sales import create_sale, get_sale
from core.stock import add_movement
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

DAY = "2026-11-01"


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _product_row(conn, pid):
    return conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    for name, role in (("owner", "owner"), ("till", "cashier"), ("store", "stock"), ("ca", "accountant")):
        conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    ensure_default_tax_rules(conn)
    pid = create_product(conn, "Cotton Shirt", hsn="6205", default_price_paise=105000, default_cost_paise=60000,
                         mrp_paise=120000, reorder_level=3)
    m_blue, l_blue = generate_variants(conn, pid, ["M", "L"], ["Blue"])
    with transaction(conn):
        add_movement(conn, m_blue, 10, "opening stock")
    yield SimpleNamespace(conn=conn, owner=1, cashier=2, stock=3, accountant=4, pid=pid, m_blue=m_blue, l_blue=l_blue)
    conn.close()


# ---------------------------------------------------------------- add

def test_add_product_with_matrix_variants_and_audit(s):
    out = pe.add_product(
        s.conn, s.owner, "Denim Jeans", category="Jeans", hsn="6203", unit="piece",
        default_price_paise=199900, sizes=["30", "32", "34"], colours=["Blue", "Black"],
    )
    assert len(out["variant_ids"]) == 6
    assert _product_row(s.conn, out["product_id"])["default_price_paise"] == 199900
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'product_created'").fetchone()[0] == 1


def test_duplicate_name_warns_and_saves_nothing_unless_confirmed(s):
    before = _count(s.conn, "products")
    with pytest.raises(pe.DuplicateProductError) as exc:
        pe.add_product(s.conn, s.owner, "cotton shirt")
    assert exc.value.existing_ids == [s.pid]
    assert _count(s.conn, "products") == before
    out = pe.add_product(s.conn, s.owner, "cotton shirt", allow_duplicate=True)  # owner reviewed and confirmed
    assert out["product_id"] != s.pid and _count(s.conn, "products") == before + 1


def test_add_product_validates_input(s):
    with pytest.raises(ValueError, match="name is required"):
        pe.add_product(s.conn, s.owner, "  ")
    with pytest.raises(ValueError, match="Unit must be"):
        pe.add_product(s.conn, s.owner, "X", unit="box")
    with pytest.raises(ValueError, match="above the MRP"):
        pe.add_product(s.conn, s.owner, "X", default_price_paise=5000, mrp_paise=4000)
    with pytest.raises(ValueError, match="Unknown product field"):
        pe.add_product(s.conn, s.owner, "X", colour="red")
    with pytest.raises(ValueError, match="between 0 and 100"):
        pe.add_product(s.conn, s.owner, "X", gst_rate_percent=150)


def test_stock_staff_can_add_products_but_not_set_prices(s):
    out = pe.add_product(s.conn, s.stock, "Kurti", sizes=["M"], colours=["Red"])
    assert _product_row(s.conn, out["product_id"])["default_price_paise"] == 0
    with pytest.raises(PermissionDenied, match="prices"):
        pe.add_product(s.conn, s.stock, "Kurti 2", default_price_paise=50000)


def test_cashier_cannot_add_or_edit_products(s):
    with pytest.raises(PermissionDenied):
        pe.add_product(s.conn, s.cashier, "Anything")
    with pytest.raises(PermissionDenied):
        pe.update_product(s.conn, s.cashier, s.pid, brand="Raymond")


# ---------------------------------------------------------------- update

def test_update_product_logs_only_what_changed(s):
    changed = pe.update_product(s.conn, s.owner, s.pid, brand="Raymond", category="Shirts", hsn="6205", reorder_level=3)
    assert changed == ["brand", "category"]  # hsn and reorder level were already the same
    row = _product_row(s.conn, s.pid)
    assert row["brand"] == "Raymond" and row["category"] == "Shirts"
    entry = s.conn.execute("SELECT before_json, after_json FROM audit_log WHERE action = 'product_updated'").fetchone()
    assert '"brand"' in entry["after_json"] and '"hsn"' not in entry["after_json"]
    assert pe.update_product(s.conn, s.owner, s.pid, brand="Raymond") == []  # nothing new, nothing logged
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'product_updated'").fetchone()[0] == 1


def test_update_product_rules(s):
    with pytest.raises(ValueError, match="name is required"):
        pe.update_product(s.conn, s.owner, s.pid, name=" ")
    with pytest.raises(ValueError, match="Use change_prices"):
        pe.update_product(s.conn, s.owner, s.pid, default_price_paise=1)
    with pytest.raises(ValueError, match="Unknown product field"):
        pe.update_product(s.conn, s.owner, s.pid, colour="x")
    with pytest.raises(ValueError, match="Product not found"):
        pe.update_product(s.conn, s.owner, 999, brand="x")
    with pytest.raises(ValueError, match="unit cannot be changed"):
        pe.update_product(s.conn, s.owner, s.pid, unit="metre")  # stock already recorded
    empty = create_product(s.conn, "Fabric roll")
    assert pe.update_product(s.conn, s.owner, empty, unit="metre") == ["unit"]  # no stock yet: allowed


def test_gst_rate_can_be_set_and_cleared(s):
    pe.update_product(s.conn, s.owner, s.pid, gst_rate_percent=5)
    assert _product_row(s.conn, s.pid)["gst_rate_percent"] == 5
    pe.update_product(s.conn, s.owner, s.pid, gst_rate_percent=None)
    assert _product_row(s.conn, s.pid)["gst_rate_percent"] is None


# ---------------------------------------------------------------- prices

def test_change_prices_needs_permission(s):
    with pytest.raises(PermissionDenied):
        pe.change_prices(s.conn, s.cashier, s.pid, price_paise=99900)
    with pytest.raises(PermissionDenied):
        pe.change_prices(s.conn, s.stock, s.pid, price_paise=99900)
    assert _product_row(s.conn, s.pid)["default_price_paise"] == 105000
    set_user_permission(s.conn, s.owner, s.stock, "change_price", True)  # the owner may grant it
    assert pe.change_prices(s.conn, s.stock, s.pid, price_paise=99900) == ["price_paise"]


def test_price_change_is_logged_with_user_before_and_after(s):
    assert pe.change_prices(s.conn, s.owner, s.pid, price_paise=110000, cost_paise=65000) == ["cost_paise", "price_paise"]
    hist = pe.price_history(s.conn, s.owner, product_id=s.pid)
    assert len(hist) == 1 and hist[0]["user"] == "owner"
    assert hist[0]["before"] == {"default_price_paise": 105000, "default_cost_paise": 60000}
    assert hist[0]["after"] == {"default_price_paise": 110000, "default_cost_paise": 65000}
    assert pe.change_prices(s.conn, s.owner, s.pid, price_paise=110000) == []  # no change, no log
    assert len(pe.price_history(s.conn, s.owner, product_id=s.pid)) == 1
    assert verify_chain(s.conn) == (True, None)


def test_price_rules(s):
    with pytest.raises(ValueError, match="above the MRP"):
        pe.change_prices(s.conn, s.owner, s.pid, price_paise=130000)  # MRP is 1,200
    with pytest.raises(ValueError, match="above the MRP"):
        pe.change_prices(s.conn, s.owner, s.pid, mrp_paise=100000)    # MRP below the current price
    with pytest.raises(ValueError, match="whole number"):
        pe.change_prices(s.conn, s.owner, s.pid, price_paise=-5)
    with pytest.raises(ValueError, match="whole number"):
        pe.change_prices(s.conn, s.owner, s.pid, price_paise=10.5)
    with pytest.raises(ValueError, match="Selling price is required"):
        pe.change_prices(s.conn, s.owner, s.pid, price_paise=None)
    with pytest.raises(ValueError, match="Unknown price"):
        pe.change_prices(s.conn, s.owner, s.pid, gst=5)
    pe.change_prices(s.conn, s.owner, s.pid, mrp_paise=None)  # MRP can be cleared
    assert _product_row(s.conn, s.pid)["mrp_paise"] is None
    pe.change_prices(s.conn, s.owner, s.pid, price_paise=500000)  # and then there is no ceiling
    assert _product_row(s.conn, s.pid)["default_price_paise"] == 500000


def test_new_price_applies_to_future_bills_old_bills_unchanged(s):
    def sell():
        return create_sale(s.conn, [{"variant_id": s.m_blue, "qty": 1}],
                           [{"mode": "cash", "amount_paise": 400000}], user_id=s.owner, on_date=DAY, mode="unregistered")

    old_id = sell()
    old_total = get_sale(s.conn, old_id)["sale"]["total_paise"]
    pe.change_prices(s.conn, s.owner, s.pid, price_paise=90000)
    new_id = sell()
    assert get_sale(s.conn, old_id)["sale"]["total_paise"] == old_total == 105000  # saved bill untouched
    assert get_sale(s.conn, new_id)["sale"]["total_paise"] == 90000


def test_variant_price_override_and_history(s):
    assert pe.set_variant_price(s.conn, s.owner, s.l_blue, 115000) is True
    assert pe.set_variant_price(s.conn, s.owner, s.l_blue, 115000) is False
    with pytest.raises(ValueError, match="above the MRP"):
        pe.set_variant_price(s.conn, s.owner, s.l_blue, 125000)
    with pytest.raises(PermissionDenied):
        pe.set_variant_price(s.conn, s.cashier, s.l_blue, 100000)
    assert pe.set_variant_price(s.conn, s.owner, s.l_blue, None) is True  # product price applies again
    hist = pe.price_history(s.conn, s.owner, variant_id=s.l_blue)
    assert [h["after"]["price_override_paise"] for h in hist] == [None, 115000]


def test_price_history_is_not_for_everyone(s):
    with pytest.raises(PermissionDenied):
        pe.price_history(s.conn, s.cashier, product_id=s.pid)
    assert pe.price_history(s.conn, s.accountant, product_id=s.pid) == []  # accountants can view cost
    with pytest.raises(ValueError, match="either a product or a variant"):
        pe.price_history(s.conn, s.owner)


# ---------------------------------------------------------------- variants and racks

def test_update_variant_alt_barcode_rack_and_reorder(s):
    assert pe.update_variant(s.conn, s.stock, s.m_blue, alt_barcode="8901234567890", rack_location="A-2", reorder_level=4) == [
        "alt_barcode", "rack_location", "reorder_level"]
    row = s.conn.execute("SELECT * FROM variants WHERE id = ?", (s.m_blue,)).fetchone()
    assert (row["alt_barcode"], row["rack_location"], row["reorder_level"]) == ("8901234567890", "A-2", 4)
    assert pe.update_variant(s.conn, s.stock, s.m_blue, rack_location="  ", reorder_level=None) == ["rack_location", "reorder_level"]
    row = s.conn.execute("SELECT * FROM variants WHERE id = ?", (s.m_blue,)).fetchone()
    assert row["rack_location"] is None and row["reorder_level"] is None


def test_supplier_barcode_cannot_clash_with_another_item(s):
    own = s.conn.execute("SELECT barcode, sku FROM variants WHERE id = ?", (s.l_blue,)).fetchone()
    with pytest.raises(ValueError, match="already used"):
        pe.update_variant(s.conn, s.owner, s.m_blue, alt_barcode=own["barcode"])
    with pytest.raises(ValueError, match="already used"):
        pe.update_variant(s.conn, s.owner, s.m_blue, alt_barcode=own["sku"].lower())
    pe.update_variant(s.conn, s.owner, s.l_blue, alt_barcode="SUP-1")
    with pytest.raises(ValueError, match="already used"):
        pe.update_variant(s.conn, s.owner, s.m_blue, alt_barcode="SUP-1")
    with pytest.raises(ValueError, match="Unknown variant field"):
        pe.update_variant(s.conn, s.owner, s.m_blue, size="XL")


def test_rack_for_many_variants_and_search_by_location(s):
    assert pe.set_rack_location(s.conn, s.stock, [s.m_blue, s.l_blue], "Shelf 3") == 2
    assert pe.set_rack_location(s.conn, s.stock, [s.m_blue, s.l_blue], "Shelf 3") == 0
    found = pe.find_variants_by_location(s.conn, s.cashier, "shelf")
    assert {r["variant_id"] for r in found} == {s.m_blue, s.l_blue}
    assert {r["variant_id"]: r["sellable_qty"] for r in found}[s.m_blue] == 10
    assert pe.find_variants_by_location(s.conn, s.cashier, "  ") == []
    with pytest.raises(ValueError, match="Variant not found"):
        pe.set_rack_location(s.conn, s.stock, [s.m_blue, 999], "Z")
    row = s.conn.execute("SELECT rack_location FROM variants WHERE id = ?", (s.m_blue,)).fetchone()
    assert row[0] == "Shelf 3"  # the failed batch changed nothing


# ---------------------------------------------------------------- active / inactive

def test_deactivate_product_keeps_history_and_blocks_selling(s):
    out = pe.set_product_active(s.conn, s.owner, s.pid, False)
    assert out == {"changed": True, "sellable_stock_left": 10}  # the screen can warn about the stock left
    assert pe.set_product_active(s.conn, s.owner, s.pid, False)["changed"] is False
    with pytest.raises(ValueError):
        create_sale(s.conn, [{"variant_id": s.m_blue, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                    user_id=s.owner, on_date=DAY, mode="unregistered")
    assert _count(s.conn, "variants") == 2  # nothing deleted
    assert pe.set_product_active(s.conn, s.owner, s.pid, True)["changed"] is True
    actions = [r[0] for r in s.conn.execute("SELECT action FROM audit_log WHERE entity = 'product' ORDER BY id")]
    assert actions == ["product_deactivated", "product_activated"]


def test_deactivate_single_variant(s):
    assert pe.set_variant_active(s.conn, s.owner, s.m_blue, False) == {"changed": True, "sellable_stock_left": 10}
    with pytest.raises(PermissionDenied):
        pe.set_variant_active(s.conn, s.cashier, s.m_blue, True)


# ---------------------------------------------------------------- bulk edit

@pytest.fixture
def three(s):
    ids = [s.pid]
    for name, price, mrp in (("Kurti", 80000, None), ("Saree", 250000, 260000)):
        ids.append(create_product(s.conn, name, default_price_paise=price, mrp_paise=mrp, season="Summer"))
    s.ids = ids
    return s


def test_bulk_preview_changes_nothing(three):
    s = three
    rows = pe.bulk_edit_products(s.conn, s.owner, s.ids, {"season": "Winter", "reorder_level": 5})
    assert {(r["name"], r["field"], r["old"], r["new"]) for r in rows} == {
        ("Cotton Shirt", "season", None, "Winter"),
        ("Cotton Shirt", "reorder_level", 3, 5),
        ("Kurti", "season", "Summer", "Winter"),
        ("Kurti", "reorder_level", 0, 5),
        ("Saree", "season", "Summer", "Winter"),
        ("Saree", "reorder_level", 0, 5),
    }
    assert _product_row(s.conn, s.pid)["season"] is None
    assert _count(s.conn, "audit_log") == 0


def test_bulk_apply_saves_and_logs_each_product(three):
    s = three
    rows = pe.bulk_edit_products(s.conn, s.owner, s.ids, {"season": "Winter", "gst_rate_percent": 5}, apply=True)
    assert len(rows) == 6
    assert all(_product_row(s.conn, i)["season"] == "Winter" for i in s.ids)
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'product_updated'").fetchone()[0] == 3


def test_bulk_percent_price_rounds_to_whole_rupee_and_logs_price_history(three):
    s = three
    pe.bulk_edit_products(s.conn, s.owner, [s.ids[1]], {"price_change_percent": -15}, apply=True)  # 800 -> 680
    assert _product_row(s.conn, s.ids[1])["default_price_paise"] == 68000
    pe.bulk_edit_products(s.conn, s.owner, [s.ids[1]], {"price_change_percent": 3.3333}, apply=True)  # 680 -> 702.66 -> 703
    assert _product_row(s.conn, s.ids[1])["default_price_paise"] == 70300
    assert len(pe.price_history(s.conn, s.owner, product_id=s.ids[1])) == 2


def test_bulk_set_price_and_mrp_ceiling_blocks_everything(three):
    s = three
    with pytest.raises(ValueError, match="above its MRP"):
        pe.bulk_edit_products(s.conn, s.owner, s.ids, {"set_price_paise": 270000}, apply=True)  # Saree MRP is 2,600
    assert _product_row(s.conn, s.pid)["default_price_paise"] == 105000  # all or nothing
    assert _count(s.conn, "audit_log") == 0
    pe.bulk_edit_products(s.conn, s.owner, [s.ids[1], s.ids[2]], {"set_price_paise": 90000}, apply=True)
    assert _product_row(s.conn, s.ids[2])["default_price_paise"] == 90000


def test_bulk_edit_rules(three):
    s = three
    with pytest.raises(PermissionDenied):
        pe.bulk_edit_products(s.conn, s.stock, s.ids, {"price_change_percent": 10})  # stock staff cannot change prices
    assert pe.bulk_edit_products(s.conn, s.stock, s.ids, {"season": "Winter"})  # but may edit other details
    with pytest.raises(PermissionDenied):
        pe.bulk_edit_products(s.conn, s.cashier, s.ids, {"season": "Winter"})
    with pytest.raises(ValueError, match="either a new price or a percent"):
        pe.bulk_edit_products(s.conn, s.owner, s.ids, {"set_price_paise": 1, "price_change_percent": 5})
    with pytest.raises(ValueError, match="between -90 and 500"):
        pe.bulk_edit_products(s.conn, s.owner, s.ids, {"price_change_percent": -100})
    with pytest.raises(ValueError, match="Unknown bulk change"):
        pe.bulk_edit_products(s.conn, s.owner, s.ids, {"brand": "x"})
    with pytest.raises(ValueError, match="Nothing to change"):
        pe.bulk_edit_products(s.conn, s.owner, s.ids, {})
    with pytest.raises(ValueError, match="at least one product"):
        pe.bulk_edit_products(s.conn, s.owner, [], {"season": "x"})
    with pytest.raises(ValueError, match="Product not found"):
        pe.bulk_edit_products(s.conn, s.owner, [s.pid, 999], {"season": "x"}, apply=True)
    assert _product_row(s.conn, s.pid)["season"] is None


def test_bulk_edit_leaves_variant_price_overrides_alone(s):
    pe.set_variant_price(s.conn, s.owner, s.l_blue, 115000)
    pe.bulk_edit_products(s.conn, s.owner, [s.pid], {"price_change_percent": -10}, apply=True)
    row = s.conn.execute("SELECT price_override_paise FROM variants WHERE id = ?", (s.l_blue,)).fetchone()
    assert row[0] == 115000
    assert _product_row(s.conn, s.pid)["default_price_paise"] == 94500
