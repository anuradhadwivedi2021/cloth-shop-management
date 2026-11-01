import pytest

from core.products import (
    create_product,
    ean13_check_digit,
    find_similar_products,
    find_variant_by_code,
    generate_variants,
    list_variants_with_stock,
    search_products,
)
from db.connection import open_db

SIZES = ["S", "M", "L", "XL", "XXL"]
COLOURS = ["Blue", "Black", "White", "Red", "Green", "Navy"]


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    yield c
    c.close()


def _shirt(conn):
    return create_product(conn, "Cotton Shirt", category="Shirts", brand="Raymond", hsn="6205")


def test_product_name_is_required(conn):
    with pytest.raises(ValueError):
        create_product(conn, "   ")


def test_matrix_creates_every_combination(conn):
    pid = _shirt(conn)
    created = generate_variants(conn, pid, SIZES, COLOURS)
    assert len(created) == 30
    rows = list_variants_with_stock(conn, pid)
    assert len(rows) == 30
    assert len({r["sku"] for r in rows}) == 30
    assert len({r["barcode"] for r in rows}) == 30
    # Each variant has its own stock record, all start at zero
    assert all(r["sellable_qty"] == 0 for r in rows)


def test_variant_barcodes_are_valid_ean13(conn):
    pid = _shirt(conn)
    generate_variants(conn, pid, ["M"], ["Blue", "Red"])
    for row in list_variants_with_stock(conn, pid):
        code = row["barcode"]
        assert len(code) == 13 and code.isdigit()
        assert ean13_check_digit(code[:12]) == code[12]


def test_matrix_twice_creates_nothing_new(conn):
    pid = _shirt(conn)
    generate_variants(conn, pid, SIZES, COLOURS)
    again = generate_variants(conn, pid, ["s", "m"], ["blue", "BLACK"])
    assert again == []
    assert len(list_variants_with_stock(conn, pid)) == 30


def test_matrix_adds_only_new_combinations(conn):
    pid = _shirt(conn)
    generate_variants(conn, pid, ["S", "M"], ["Red"])
    new = generate_variants(conn, pid, ["S", "M", "L"], ["Red"])
    assert len(new) == 1
    assert len(list_variants_with_stock(conn, pid)) == 3


def test_blank_sizes_are_rejected(conn):
    pid = _shirt(conn)
    with pytest.raises(ValueError):
        generate_variants(conn, pid, ["", "  "], ["Red"])
    assert list_variants_with_stock(conn, pid) == []


def test_unknown_product_is_rejected(conn):
    with pytest.raises(ValueError):
        generate_variants(conn, 999, ["M"], ["Red"])


def test_duplicate_product_warning(conn):
    _shirt(conn)
    matches = find_similar_products(conn, "  cotton shirt ")
    assert len(matches) == 1
    assert matches[0]["name"] == "Cotton Shirt"
    assert find_similar_products(conn, "Silk Saree") == []


def test_find_variant_by_barcode_or_sku(conn):
    pid = _shirt(conn)
    generate_variants(conn, pid, ["M"], ["Blue"])
    variant = list_variants_with_stock(conn, pid)[0]
    assert find_variant_by_code(conn, variant["barcode"])["id"] == variant["id"]
    assert find_variant_by_code(conn, variant["sku"].lower())["id"] == variant["id"]
    assert find_variant_by_code(conn, "no-such-code") is None


def test_search_finds_by_name_size_and_colour(conn):
    pid = _shirt(conn)
    generate_variants(conn, pid, ["XL"], ["Navy"])
    create_product(conn, "Silk Saree", category="Sarees")
    assert [r["name"] for r in search_products(conn, "shirt")] == ["Cotton Shirt"]
    assert [r["name"] for r in search_products(conn, "navy")] == ["Cotton Shirt"]
    assert [r["name"] for r in search_products(conn, "xl")] == ["Cotton Shirt"]
    assert len(search_products(conn, "")) == 2


def test_ean13_check_digit_known_value(conn):
    assert ean13_check_digit("400638133393") == "1"  