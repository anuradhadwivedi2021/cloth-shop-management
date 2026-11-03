"""Guards against half-wired migrations (a migration file that is never applied)."""

import re
from pathlib import Path

from db.connection import open_db
from db.schema import MIGRATIONS

DB_FOLDER = Path(__file__).resolve().parent.parent / "db"


def test_every_migration_file_is_registered_and_nothing_is_missing():
    files = {int(m.group(1)) for p in DB_FOLDER.glob("migration_*.py") if (m := re.match(r"migration_(\d+)\.py", p.name))}
    inline = {1, 2, 3}  # defined inside db/schema.py
    assert files | inline == set(MIGRATIONS), "a migration file exists but is not in MIGRATIONS (or the reverse)"


def test_migration_10_is_deliberately_unused():
    # Fabric by the metre (milli-unit quantities) is a later phase. Its rewrite of every quantity
    # must not be reintroduced without converting sales, returns, stock and reports together.
    assert 10 not in MIGRATIONS


def test_fresh_database_has_the_columns_later_features_need(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    try:
        def cols(table):
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert {"balance_paise", "initial_amount_paise"} <= cols("gift_vouchers")
        assert {"amount_paise", "doc_type", "doc_id", "note"} <= cols("store_credit_ledger")
        assert {"total_amount_paise", "paid_amount_paise", "due_date"} <= cols("customer_dues_ledger")
        assert "credit_enabled" in cols("customers")
        assert "salesperson_id" in cols("sales")
    finally:
        conn.close()
