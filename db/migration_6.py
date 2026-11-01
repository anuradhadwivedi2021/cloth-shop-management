"""Schema migration 6 (Step 12): customers module.

* customers get an `anonymised_at` marker (set when personal data is erased on request)
  and an `updated_at` time.
* customer_access_log: who opened or exported a customer's personal data, and when.
  Append-only, and it holds only ids and action names, never personal details.
* A customer row can never be hard-deleted. Customers are archived (is_active = 0) or
  anonymised, so old bills and credit notes always keep a valid reference.
"""

MIGRATION_6 = """
ALTER TABLE customers ADD COLUMN anonymised_at TEXT;
ALTER TABLE customers ADD COLUMN updated_at TEXT;

CREATE TABLE customer_access_log (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_customer_access_customer ON customer_access_log(customer_id);

CREATE TRIGGER customer_access_log_no_update BEFORE UPDATE ON customer_access_log
BEGIN SELECT RAISE(ABORT, 'customer access log is append-only'); END;
CREATE TRIGGER customer_access_log_no_delete BEFORE DELETE ON customer_access_log
BEGIN SELECT RAISE(ABORT, 'customer access log cannot be deleted'); END;

CREATE TRIGGER customers_no_delete BEFORE DELETE ON customers
BEGIN SELECT RAISE(ABORT, 'customers are archived or anonymised, never deleted'); END;
"""
