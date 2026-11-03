import pytest

from core.matrix_entry import preview_matrix, save_matrix_product, split_list
from core.permissions import PermissionDenied
from core.product_edit import DuplicateProductError
from core.stock import get_stock
from db.connection import open_db


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    for name, role in (("owner", "owner"), ("till", "cashier")):
        c.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    yield c
    c.close()


def test_split_list_cleans_and_keeps_order():
    assert split_list(" S, M ,, m, L ,S") == ["S", "M", "L"]
    assert split_list("") == [] and split_list(None) == []


def test_preview_is_every_size_colour_pair():
    combos = preview_matrix("S,M", "Blue,Black,White")
    assert len(combos) == 6 and ("M", "White") in combos
    with pytest.raises(ValueError):
        preview_matrix("S,M", " , ")


def test_five_sizes_six_colours_makes_thirty_separate_variants(conn):
    out = save_matrix_product(
        conn, 1, name="Slim Shirt", category="Shirts", price_text="1,049.99", cost_text="600",
        sizes_text="S,M,L,XL,XXL", colours_text="Blue,Black,White,Red,Green,Grey",
    )
    ids = out["variant_ids"]
    assert len(ids) == 30
    rows = conn.execute("SELECT sku, barcode FROM variants WHERE product_id = ?", (out["product_id"],)).fetchall()
    assert len({r["sku"] for r in rows}) == 30 and len({r["barcode"] for r in rows}) == 30
    assert all(len(r["barcode"]) == 13 for r in rows)
    assert all(get_stock(conn, v) == 0 for v in ids)  # stock only arrives through purchases/movements


def test_price_is_exact_paise_not_float(conn):
    out = save_matrix_product(conn, 1, name="Kurti", price_text="1049.99", cost_text="19.99",
                              sizes_text="M", colours_text="Pink")
    row = conn.execute("SELECT default_price_paise, default_cost_paise FROM products WHERE id = ?",
                       (out["product_id"],)).fetchone()
    assert (row[0], row[1]) == (104999, 1999)


def test_bad_price_saves_nothing(conn):
    with pytest.raises(ValueError, match="Selling price"):
        save_matrix_product(conn, 1, name="Kurti", price_text="12.345", sizes_text="M", colours_text="Pink")
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_duplicate_name_warns_then_allows_when_confirmed(conn):
    save_matrix_product(conn, 1, name="Kurti", sizes_text="M", colours_text="Pink")
    with pytest.raises(DuplicateProductError):
        save_matrix_product(conn, 1, name="kurti ", sizes_text="L", colours_text="Blue")
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
    save_matrix_product(conn, 1, name="Kurti", sizes_text="L", colours_text="Blue", allow_duplicate=True)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


def test_cashier_cannot_add_products_and_nothing_is_saved(conn):
    with pytest.raises(PermissionDenied):
        save_matrix_product(conn, 2, name="Kurti", sizes_text="M", colours_text="Pink")
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_metre_is_not_offered_until_fabric_support_exists(conn):
    with pytest.raises(ValueError):
        save_matrix_product(conn, 1, name="Silk", unit="metre", sizes_text="Free", colours_text="Red")


def test_creating_a_product_is_audited(conn):
    save_matrix_product(conn, 1, name="Kurti", sizes_text="M", colours_text="Pink")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'product_created'").fetchone()[0] == 1
