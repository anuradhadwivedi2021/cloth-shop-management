"""Schema migration 8 (Step 13b): supplier payments and supplier details.

* supplier_payments: every payment made to a supplier, either against one purchase bill
  or "on account" (advance, no bill). A reversal is a new linked negative row. Rows are
  never edited or deleted. Each row also has a matching row in `payments`
  (document_type 'purchase'), so cash and payment-mode reports include it.
* suppliers get an account reference and an updated-at time, and can never be hard-deleted
  (they are deactivated), so old purchases always keep a valid supplier.
"""

MIGRATION_8 = """
ALTER TABLE suppliers ADD COLUMN account_reference TEXT;
ALTER TABLE suppliers ADD COLUMN updated_at TEXT;

CREATE TRIGGER suppliers_no_delete BEFORE DELETE ON suppliers
BEGIN SELECT RAISE(ABORT, 'suppliers are deactivated, never deleted'); END;

CREATE TABLE supplier_payments (
    id              INTEGER PRIMARY KEY,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    purchase_id     INTEGER REFERENCES purchases(id),
    voucher_no      TEXT UNIQUE,
    payment_date    TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('cash','upi','card','bank_transfer','cheque','other')),
    amount_paise    INTEGER NOT NULL CHECK (amount_paise != 0),
    reference       TEXT,
    notes           TEXT,
    payment_id      INTEGER REFERENCES payments(id),
    reversal_of     INTEGER REFERENCES supplier_payments(id),
    reason          TEXT,
    idempotency_key TEXT UNIQUE,
    user_id         INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK ((amount_paise < 0) = (reversal_of IS NOT NULL))
);
CREATE INDEX idx_supplier_payments_supplier ON supplier_payments(supplier_id);
CREATE INDEX idx_supplier_payments_purchase ON supplier_payments(purchase_id);
CREATE UNIQUE INDEX idx_supplier_payments_one_reversal ON supplier_payments(reversal_of)
    WHERE reversal_of IS NOT NULL;

CREATE TRIGGER supplier_payments_no_update BEFORE UPDATE ON supplier_payments
BEGIN SELECT RAISE(ABORT, 'supplier payments cannot be edited; add a reversal'); END;
CREATE TRIGGER supplier_payments_no_delete BEFORE DELETE ON supplier_payments
BEGIN SELECT RAISE(ABORT, 'supplier payments cannot be deleted; add a reversal'); END;
"""
