"""PRD-10 and the SRS test "Import a CSV with 5% bad rows: verify preview, error file and no partial import"."""

import csv

import pytest

from core import auth, import_products as ip, shop
from core.audit import verify_chain
from core.permissions import PermissionDenied
from core.stock import get_stock
from db.connection import open_db

HEAD = "name,size,colour,category,brand,hsn,gst,mrp,price,cost,stock\n"


@pytest.fixture
def world(tmp_path):
    folder = tmp_path / "shop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    stock_user = auth.create_user(conn, owner.user_id, "stocky", "stock-pass-1", "stock")
    till_user = auth.create_user(conn, owner.user_id, "till", "till-pass-1", "cashier")
    yield conn, owner.user_id, stock_user, till_user, tmp_path
    conn.close()


def _file(tmp_path, text, name="items.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- a good file

def test_the_template_imports_cleanly(world):
    conn, owner, *_, tmp = world
    path = ip.write_template(tmp / "template.csv")
    preview = ip.preview_import(conn, owner, path)
    assert preview["can_import"] and preview["problems"] == []
    assert (preview["new_products"], preview["new_variants"], preview["opening_pieces"]) == (1, 2, 18)
    assert _count(conn, "products") == 0                      # a preview saves nothing
    result = ip.import_products(conn, owner, path)
    assert result == {"products": 1, "variants": 2, "added_to_existing": 0, "opening_pieces": 18}
    product = conn.execute("SELECT * FROM products").fetchone()
    assert (product["name"], product["brand"], product["hsn"], product["gst_rate_percent"]) == ("Cotton Shirt", "Raymond", "6205", 5)
    assert (product["mrp_paise"], product["default_price_paise"], product["default_cost_paise"]) == (199900, 105000, 60000)
    variants = conn.execute("SELECT * FROM variants ORDER BY id").fetchall()
    assert [(v["size"], v["colour"], v["rack_location"], v["reorder_level"]) for v in variants] == [("M", "Blue", "A1", 3), ("L", "Blue", "A1", 3)]
    assert all(v["barcode"] and v["sku"] and v["price_override_paise"] is None for v in variants)
    assert [get_stock(conn, v["id"]) for v in variants] == [10, 8]
    assert verify_chain(conn)[0] is True
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'products_imported'").fetchone()[0] == 1


def test_a_different_price_for_one_size_becomes_that_variants_own_price(world):
    conn, owner, *_, tmp = world
    path = _file(tmp, HEAD + "Kurta,M,Red,,,,,,500,,\nKurta,XXL,Red,,,,,,650,,\n")
    ip.import_products(conn, owner, path)
    rows = {r["size"]: r["price_override_paise"] for r in conn.execute("SELECT size, price_override_paise FROM variants")}
    assert rows == {"M": None, "XXL": 65000}
    assert conn.execute("SELECT default_price_paise FROM products").fetchone()[0] == 50000


def test_header_names_and_excel_style_files_are_understood(world):
    conn, owner, *_, tmp = world
    path = _file(tmp, "Product Name;Size;Color;Selling Price;Opening Stock;Barcode\nशर्ट;M;नीला;799.50;4;8901234567890\n", encoding="cp1252".replace("cp1252", "utf-8"))
    ip.import_products(conn, owner, path)
    row = conn.execute("SELECT p.name, p.default_price_paise, v.colour, v.barcode FROM variants v JOIN products p ON p.id = v.product_id").fetchone()
    assert tuple(row) == ("शर्ट", 79950, "नीला", "8901234567890")


def test_a_windows_excel_csv_is_read(world):
    conn, owner, *_, tmp = world
    path = _file(tmp, HEAD + "Café Shirt,M,Blue,,,,,,100,,\n", encoding="cp1252")
    ip.import_products(conn, owner, path)
    assert conn.execute("SELECT name FROM products").fetchone()[0] == "Café Shirt"


def test_an_excel_file_is_read_and_a_numeric_barcode_stays_whole(world):
    openpyxl = pytest.importorskip("openpyxl")
    conn, owner, *_, tmp = world
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["name", "size", "colour", "price", "stock", "barcode"])
    sheet.append(["Jeans", "32", "Black", 1299.5, 6, 8901234567890])
    path = tmp / "items.xlsx"
    book.save(path)
    ip.import_products(conn, owner, path)
    row = conn.execute("SELECT p.default_price_paise, v.barcode FROM variants v JOIN products p ON p.id = v.product_id").fetchone()
    assert tuple(row) == (129950, "8901234567890")


# ---------------------------------------------------------------- bad files: preview, error file, no partial import

def test_five_percent_bad_rows_show_in_the_preview_and_nothing_is_imported(world):
    conn, owner, *_, tmp = world
    lines = [f"Shirt {i},M,Blue,,,,,,{500 + i},,5" for i in range(95)]
    lines += [
        "Bad A,,Blue,,,,,,100,,1",                       # no size
        "Bad B,M,Blue,,,,,,12.345,,1",                    # 3 decimals in a price
        "Shirt 1,M,Blue,,,,,,100,,1",                     # the same variant twice in the file
        "Bad D,M,Blue,,,,150,,100,,1",                    # GST 150
        "Bad E,M,Blue,,,,,,100,,-4",                      # negative stock
    ]
    path = _file(tmp, HEAD + "\n".join(lines) + "\n")
    preview = ip.preview_import(conn, owner, path)
    assert preview["total_rows"] == 100 and preview["bad_rows"] == 5 and not preview["can_import"]
    assert sorted(p["row"] for p in preview["problems"]) == [97, 98, 99, 100, 101]
    with pytest.raises(ValueError, match="Nothing was imported"):
        ip.import_products(conn, owner, path)
    assert (_count(conn, "products"), _count(conn, "variants"), _count(conn, "stock_movements")) == (0, 0, 0)

    error_file = ip.write_error_file(preview, tmp / "errors.csv")
    with open(error_file, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][:3] == ["file row", "problem in column", "problem"] and len(rows) == 6
    assert {r[0] for r in rows[1:]} == {"97", "98", "99", "100", "101"} and all(r[2] for r in rows[1:])

    # fix only the bad rows (drop them) and the rest goes in
    good = _file(tmp, HEAD + "\n".join(lines[:95]) + "\n", name="fixed.csv")
    assert ip.import_products(conn, owner, good)["variants"] == 95


def test_a_clash_with_an_existing_variant_or_code_is_an_error_and_nothing_is_overwritten(world):
    conn, owner, *_, tmp = world
    ip.import_products(conn, owner, _file(tmp, HEAD + "Shirt,M,Blue,,,,,,500,,2\n", name="a.csv"))
    before = conn.execute("SELECT barcode, sku FROM variants").fetchone()
    second = _file(tmp, HEAD.rstrip() + ",barcode\nSHIRT,m,BLUE,,,,,,999,,5,\nOther,L,Red,,,,,,10,,1," + before["barcode"] + "\n", name="b.csv")
    preview = ip.preview_import(conn, owner, second)
    assert {(p["row"], p["column"]) for p in preview["problems"]} == {(2, "size"), (3, "barcode")}
    with pytest.raises(ValueError):
        ip.import_products(conn, owner, second)
    assert conn.execute("SELECT default_price_paise FROM products").fetchone()[0] == 50000
    assert _count(conn, "variants") == 1


def test_an_existing_product_name_gets_only_the_new_variants(world):
    conn, owner, *_, tmp = world
    ip.import_products(conn, owner, _file(tmp, HEAD + "Shirt,M,Blue,,Raymond,,,,500,,2\n", name="a.csv"))
    result = ip.import_products(conn, owner, _file(tmp, HEAD + "Shirt,L,Blue,,Other Brand,,,,600,,3\n", name="b.csv"))
    assert result["products"] == 0 and result["added_to_existing"] == 1 and result["variants"] == 1
    assert _count(conn, "products") == 1 and _count(conn, "variants") == 2
    assert conn.execute("SELECT brand FROM products").fetchone()[0] == "Raymond"      # its own details are left alone


def test_rows_of_one_product_that_disagree_are_reported(world):
    conn, owner, *_, tmp = world
    path = _file(tmp, HEAD + "Shirt,M,Blue,Men,Raymond,,,,500,,1\nShirt,L,Blue,Women,Raymond,,,,500,,1\n")
    problems = ip.preview_import(conn, owner, path)["problems"]
    assert [(p["row"], p["column"]) for p in problems] == [(3, "category")]


def test_a_file_without_the_needed_columns_or_of_the_wrong_kind_is_refused(world):
    conn, owner, *_, tmp = world
    with pytest.raises(ValueError, match="Missing: size, colour"):
        ip.preview_import(conn, owner, _file(tmp, "name,price\nShirt,100\n"))
    with pytest.raises(ValueError, match="empty"):
        ip.preview_import(conn, owner, _file(tmp, ""))
    with pytest.raises(ValueError, match=r"\.csv or \.xlsx"):
        ip.preview_import(conn, owner, _file(tmp, "x", name="items.txt"))
    with pytest.raises(ValueError, match="not found"):
        ip.preview_import(conn, owner, tmp / "nothing.csv")
    with pytest.raises(ValueError, match="same thing"):
        ip.preview_import(conn, owner, _file(tmp, "name,size,colour,color\nA,M,Blue,Red\n"))


def test_blank_lines_are_ignored_and_the_row_numbers_stay_true_to_the_file(world):
    conn, owner, *_, tmp = world
    path = _file(tmp, HEAD + "\n\nShirt,,Blue,,,,,,100,,1\n")
    assert [p["row"] for p in ip.preview_import(conn, owner, path)["problems"]] == [4]


# ---------------------------------------------------------------- who may import

def test_only_people_who_may_edit_products_can_import(world):
    conn, owner, stock_user, till_user, tmp = world
    path = _file(tmp, HEAD + "Shirt,M,Blue,,,,,,100,,\n")
    with pytest.raises(PermissionDenied):
        ip.preview_import(conn, till_user, path)
    with pytest.raises(PermissionDenied):
        ip.import_products(conn, till_user, path)
    assert ip.import_products(conn, stock_user, path)["variants"] == 1       # stock staff may


def test_cost_needs_the_see_cost_permission(world):
    conn, owner, stock_user, _, tmp = world
    path = _file(tmp, HEAD + "Shirt,M,Blue,,,,,,100,50,\n")
    problems = ip.preview_import(conn, stock_user, path)["problems"]
    assert [(p["row"], p["column"]) for p in problems] == [(2, "cost")]
    assert ip.import_products(conn, owner, path)["variants"] == 1             # the owner may


def test_a_failure_halfway_leaves_nothing_behind(world, monkeypatch):
    conn, owner, *_, tmp = world
    path = _file(tmp, HEAD + "A,M,Blue,,,,,,100,,1\nB,M,Blue,,,,,,100,,1\n")
    real = ip.add_movement
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk problem")
        return real(*args, **kwargs)

    monkeypatch.setattr(ip, "add_movement", boom)
    with pytest.raises(RuntimeError):
        ip.import_products(conn, owner, path)
    assert (_count(conn, "products"), _count(conn, "variants"), _count(conn, "stock_movements")) == (0, 0, 0)
    assert conn.in_transaction is False
    assert verify_chain(conn)[0] is True


def test_a_big_file_imports_in_a_sensible_time(world):
    import time

    conn, owner, *_, tmp = world
    lines = [f"Item {i // 5},{['S', 'M', 'L', 'XL', 'XXL'][i % 5]},Blue,,,,,,500,,3" for i in range(2000)]
    path = _file(tmp, HEAD + "\n".join(lines) + "\n")
    started = time.perf_counter()
    result = ip.import_products(conn, owner, path)
    assert result["variants"] == 2000 and result["products"] == 400
    assert time.perf_counter() - started < 20
