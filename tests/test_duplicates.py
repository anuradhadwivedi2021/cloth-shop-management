"""PRD-06: warn about a duplicate SKU or barcode before saving. Nothing is merged or written."""

import pytest

from core import auth
from core.duplicates import find_duplicate_codes
from core.permissions import PermissionDenied, set_user_permission
from core.product_edit import add_product, update_variant
from db.connection import open_db


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    shirt = add_product(conn, owner, "Shirt", sizes=["M", "L"], colours=["Blue"])
    kurta = add_product(conn, owner, "Kurta", sizes=["M"], colours=["Red"])
    v_m, v_l = shirt["variant_ids"]
    update_variant(conn, owner, v_m, alt_barcode="SUP-1001")
    row = conn.execute("SELECT sku, barcode FROM variants WHERE id = ?", (v_l,)).fetchone()
    yield type("S", (), {"conn": conn, "owner": owner, "till": till, "v_m": v_m, "v_l": v_l,
                         "k": kurta["variant_ids"][0], "sku_l": row["sku"], "barcode_l": row["barcode"]})
    conn.close()


def test_no_clash_gives_an_empty_list(s):
    assert find_duplicate_codes(s.conn, s.owner, sku="NEW-SKU", barcode="9999999999999", alt_barcode="SUP-2") == []
    assert find_duplicate_codes(s.conn, s.owner) == []


def test_sku_and_barcode_clashes_name_the_item_that_has_the_code(s):
    found = find_duplicate_codes(s.conn, s.owner, sku=s.sku_l.lower(), barcode=s.barcode_l)
    assert [(f["typed_as"], f["found_in"], f["variant_id"]) for f in found] == [
        ("sku", "sku", s.v_l), ("barcode", "barcode", s.v_l)]
    assert found[0]["product_name"] == "Shirt" and (found[0]["size"], found[0]["colour"]) == ("L", "Blue")


def test_a_supplier_barcode_equal_to_another_items_own_code_is_flagged(s):
    found = find_duplicate_codes(s.conn, s.owner, alt_barcode=s.barcode_l)
    assert [(f["typed_as"], f["found_in"], f["variant_id"]) for f in found] == [("alt_barcode", "barcode", s.v_l)]
    found = find_duplicate_codes(s.conn, s.owner, barcode="sup-1001")           # other item's supplier code, any case
    assert [(f["found_in"], f["variant_id"]) for f in found] == [("alt_barcode", s.v_m)]


def test_editing_an_item_does_not_clash_with_itself(s):
    assert find_duplicate_codes(s.conn, s.owner, sku=s.sku_l, exclude_variant_id=s.v_l) == []
    assert len(find_duplicate_codes(s.conn, s.owner, sku=s.sku_l, exclude_variant_id=s.v_m)) == 1


def test_it_only_warns_and_changes_nothing(s):
    before = s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], s.conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
    find_duplicate_codes(s.conn, s.owner, sku=s.sku_l)
    assert (s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], s.conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0]) == before


def test_needs_permission_to_see_products(s):
    assert find_duplicate_codes(s.conn, s.till, sku=s.sku_l)             # cashier may look
    set_user_permission(s.conn, s.owner, s.till, "view_products", False)
    with pytest.raises(PermissionDenied):
        find_duplicate_codes(s.conn, s.till, sku=s.sku_l)