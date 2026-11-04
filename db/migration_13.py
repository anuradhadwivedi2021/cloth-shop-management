# db/migration_13.py
"""Schema migration 13: credit sales (customer dues).

Dues are NOT stored here. A bill's due is always: bill total - payments on the bill - dues cleared
by returns (single source of truth). This migration only adds what that cannot express:

1. customer_receipts            numbered receipts (RCT series) for money received from a customer.
                                A receipt is never edited or deleted; a mistake is cancelled by a
                                reversal receipt (negative amount, points at the original).
2. customer_receipt_allocations which bill(s) a receipt paid, and the payments row it created.
3. sale_due_dates               promised pay-by date of a credit bill. Append-only history: the
                                latest row is the current due date. Saved bills stay untouched.

customer_dues_ledger (migration 12) is left in place but is no longer used by any code.
"""

MIGRATION_13 = """
CREATE TABLE customer_receipts (
    id              INTEGER PRIMARY KEY,
    series          TEXT NOT NULL,
    number          INTEGER NOT NULL,
    receipt_no      TEXT NOT NULL UNIQUE,
    fin_year        TEXT NOT NULL,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    mode            TEXT NOT NULL CHECK (mode IN ('cash','upi','card','bank_transfer','cheque','other')),
    amount_paise    INTEGER NOT NULL CHECK (amount_paise != 0),
    reference       TEXT,
    reversal_of     INTEGER UNIQUE REFERENCES customer_receipts(id),
    reason          TEXT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    idempotency_key TEXT UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (series, fin_year, number),
    CHECK ((reversal_of IS NULL AND amount_paise > 0) OR (reversal_of IS NOT NULL AND amount_paise < 0))
);
CREATE INDEX idx_customer_receipts_customer ON customer_receipts(customer_id);

CREATE TABLE customer_receipt_allocations (
    id           INTEGER PRIMARY KEY,
    receipt_id   INTEGER NOT NULL REFERENCES customer_receipts(id),
    sale_id      INTEGER NOT NULL REFERENCES sales(id),
    payment_id   INTEGER NOT NULL UNIQUE REFERENCES payments(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise != 0)
);
CREATE INDEX idx_receipt_alloc_receipt ON customer_receipt_allocations(receipt_id);
CREATE INDEX idx_receipt_alloc_sale ON customer_receipt_allocations(sale_id);

CREATE TABLE sale_due_dates (
    id         INTEGER PRIMARY KEY,
    sale_id    INTEGER NOT NULL REFERENCES sales(id),
    due_date   TEXT NOT NULL CHECK (due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    reason     TEXT,
    user_id    INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_sale_due_dates_sale ON sale_due_dates(sale_id);

CREATE TRIGGER customer_receipts_no_update BEFORE UPDATE ON customer_receipts
BEGIN SELECT RAISE(ABORT, 'receipts cannot be edited; add a reversal receipt'); END;
CREATE TRIGGER customer_receipts_no_delete BEFORE DELETE ON customer_receipts
BEGIN SELECT RAISE(ABORT, 'receipts cannot be deleted; add a reversal receipt'); END;
CREATE TRIGGER customer_receipt_alloc_no_update BEFORE UPDATE ON customer_receipt_allocations
BEGIN SELECT RAISE(ABORT, 'receipt allocations are append-only'); END;
CREATE TRIGGER customer_receipt_alloc_no_delete BEFORE DELETE ON customer_receipt_allocations
BEGIN SELECT RAISE(ABORT, 'receipt allocations are append-only'); END;
CREATE TRIGGER sale_due_dates_no_update BEFORE UPDATE ON sale_due_dates
BEGIN SELECT RAISE(ABORT, 'due date history is append-only'); END;
CREATE TRIGGER sale_due_dates_no_delete BEFORE DELETE ON sale_due_dates
BEGIN SELECT RAISE(ABORT, 'due date history is append-only'); END;
"""
