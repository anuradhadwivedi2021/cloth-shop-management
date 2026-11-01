import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from db.connection import WrongPasswordError, open_db, transaction
from db.schema import MIGRATION_1, MIGRATIONS


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    yield c
    c.close()


def _seed(conn):
    """Insert one user, product and variant. Returns (user_id, variant_id)."""
    conn.execute(
        "INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')"
    )
    conn.execute("INSERT INTO products(name) VALUES ('Shirt')")
    conn.execute(
        "INSERT INTO variants(product_id, size, colour, sku) VALUES (1, 'M', 'Blue', 'SH-M-BLU')"
    )
    return 1, 1


def _add_sale(conn):
    conn.execute(
        "INSERT INTO sales(series, number, invoice_no, fin_year, user_id, tax_mode, "
        "subtotal_paise, taxable_paise, total_paise) "
        "VALUES ('INV', 1, 'INV/26-27/000001', '26-27', 1, 'regular', 105000, 100000, 105000)"
    )


def test_database_file_is_encrypted(tmp_path):
    path = tmp_path / "shop.db"
    open_db(path, "secret").close()
    header = path.read_bytes()[:16]
    assert header != b"SQLite format 3\x00"


def test_wrong_password_is_rejected(tmp_path):
    path = tmp_path / "shop.db"
    open_db(path, "right-password").close()
    with pytest.raises(WrongPasswordError):
        open_db(path, "wrong-password")


def test_correct_password_reopens(tmp_path):
    path = tmp_path / "shop.db"
    open_db(path, "pw").close()
    c = open_db(path, "pw")
    assert c.execute("SELECT count(*) FROM products").fetchone()[0] == 0
    c.close()


def test_all_core_tables_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    expected = {
        "settings", "users", "audit_log", "products", "variants", "suppliers",
        "customers", "tax_rules", "number_series", "purchases", "purchase_lines",
        "sales", "sale_lines", "payments", "stock_movements", "variant_stock",
        "schema_version", "held_bills", "sale_returns", "sale_return_lines", "store_credit_ledger",
        "customer_access_log",
    }
    assert expected <= names


def test_migration_runs_only_once(tmp_path):
    path = tmp_path / "shop.db"
    open_db(path, "pw").close()
    c = open_db(path, "pw")
    versions = [r[0] for r in c.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == sorted(MIGRATIONS)  # each migration recorded exactly once
    c.close()


def test_old_database_is_upgraded_without_losing_data(tmp_path):
    """A database created at schema version 1 gets the later migrations and keeps its rows."""
    path = tmp_path / "shop.db"
    raw = sqlcipher.connect(str(path))
    raw.execute("PRAGMA key = 'pw'")
    raw.executescript(
        "BEGIN;" + MIGRATION_1 + "CREATE TABLE schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));"
        "INSERT INTO schema_version(version) VALUES (1);"
        "INSERT INTO products(name) VALUES ('Old shirt');"
        "COMMIT;"
    )
    raw.close()

    c = open_db(path, "pw")
    assert c.execute("SELECT name FROM products").fetchone()[0] == "Old shirt"
    assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == max(MIGRATIONS)
    c.execute("SELECT prices_include_tax, tendered_paise FROM sales")  # new columns exist
    c.execute("SELECT slot FROM held_bills")  # new table exists
    c.close()


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlcipher.IntegrityError):
        conn.execute(
            "INSERT INTO variants(product_id, size, colour, sku) VALUES (999, 'M', 'Red', 'X')"
        )


def test_same_size_and_colour_cannot_be_duplicated(conn):
    _seed(conn)
    with pytest.raises(sqlcipher.IntegrityError):
        conn.execute(
            "INSERT INTO variants(product_id, size, colour, sku) VALUES (1, 'M', 'Blue', 'OTHER')"
        )


def test_stock_movements_are_append_only(conn):
    _, vid = _seed(conn)
    conn.execute(
        "INSERT INTO stock_movements(variant_id, qty_change, reason, balance_after) "
        "VALUES (?, 10, 'purchase', 10)", (vid,)
    )
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("UPDATE stock_movements SET qty_change = 99")
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("DELETE FROM stock_movements")


def test_variant_stock_view_sums_movements(conn):
    _, vid = _seed(conn)
    conn.execute(
        "INSERT INTO stock_movements(variant_id, qty_change, reason, balance_after) VALUES (?, 10, 'purchase', 10)",
        (vid,),
    )
    conn.execute(
        "INSERT INTO stock_movements(variant_id, qty_change, reason, balance_after) VALUES (?, -3, 'sale', 7)",
        (vid,),
    )
    conn.execute(
        "INSERT INTO stock_movements(variant_id, qty_change, bucket, reason, balance_after) "
        "VALUES (?, 1, 'damaged', 'damage', 1)",
        (vid,),
    )
    row = conn.execute("SELECT * FROM variant_stock WHERE variant_id = ?", (vid,)).fetchone()
    assert row["sellable_qty"] == 7
    assert row["damaged_qty"] == 1


def test_saved_bill_cannot_be_deleted_or_changed(conn):
    _seed(conn)
    _add_sale(conn)
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("DELETE FROM sales")
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("UPDATE sales SET total_paise = 1")


def test_payments_cannot_be_edited_or_deleted(conn):
    _seed(conn)
    conn.execute(
        "INSERT INTO payments(document_type, document_id, mode, amount_paise) VALUES ('sale', 1, 'cash', 105000)"
    )
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("UPDATE payments SET amount_paise = 1")
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("DELETE FROM payments")


def test_confirmed_purchase_lines_are_locked(conn):
    _seed(conn)
    conn.execute("INSERT INTO suppliers(name) VALUES ('ABC Textiles')")
    conn.execute(
        "INSERT INTO purchases(supplier_id, invoice_no, invoice_date, fin_year, status) "
        "VALUES (1, 'S-1', '2026-11-01', '26-27', 'draft')"
    )
    conn.execute(
        "INSERT INTO purchase_lines(purchase_id, variant_id, qty, unit_cost_paise, line_total_paise) "
        "VALUES (1, 1, 5, 50000, 250000)"
    )
    # Draft lines can still be edited
    conn.execute("UPDATE purchase_lines SET qty = 6 WHERE id = 1")
    conn.execute("UPDATE purchases SET status = 'confirmed' WHERE id = 1")
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("UPDATE purchase_lines SET qty = 7 WHERE id = 1")
    with pytest.raises(sqlcipher.DatabaseError):
        conn.execute("DELETE FROM purchases WHERE id = 1")


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO products(name) VALUES ('Will vanish')")
            raise RuntimeError("crash mid-save")
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 0


def test_transaction_commits_on_success(conn):
    with transaction(conn):
        conn.execute("INSERT INTO products(name) VALUES ('Kept')")
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 1