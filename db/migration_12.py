# db/migration_12.py
"""Schema migration 12 (Step 4 of Phase A): Credit sales, credit limits, and dues ledger.

1. Adds credit sales configuration to customers table (credit_limit_paise, credit_days).
2. Creates customer_dues_ledger to track unpaid invoices, part-payments, and ageing (0-30, 31-60, 60+ days).
"""

MIGRATION_12 = """
-- 1. Add Credit Limits to Customers Table
ALTER TABLE customers ADD COLUMN credit_enabled INTEGER NOT NULL DEFAULT 0 CHECK (credit_enabled IN (0, 1));
ALTER TABLE customers ADD COLUMN credit_limit_paise INTEGER NOT NULL DEFAULT 0 CHECK (credit_limit_paise >= 0);
ALTER TABLE customers ADD COLUMN default_credit_days INTEGER NOT NULL DEFAULT 30 CHECK (default_credit_days >= 0);

-- 2. Customer Dues Ledger for Tracking Unpaid Bills & Part-Payments
CREATE TABLE customer_dues_ledger (
    id                 INTEGER PRIMARY KEY,
    customer_id        INTEGER NOT NULL REFERENCES customers(id),
    sale_id            INTEGER NOT NULL REFERENCES sales(id),
    total_amount_paise INTEGER NOT NULL CHECK (total_amount_paise >= 0),
    paid_amount_paise  INTEGER NOT NULL DEFAULT 0 CHECK (paid_amount_paise >= 0),
    due_date           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'UNPAID' CHECK (status IN ('UNPAID', 'PARTIAL', 'PAID')),
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_customer_dues_customer ON customer_dues_ledger(customer_id);
CREATE INDEX idx_customer_dues_status ON customer_dues_ledger(status);
CREATE INDEX idx_customer_dues_due_date ON customer_dues_ledger(due_date);

-- Immutability & Audit Safeguards
CREATE TRIGGER customer_dues_ledger_no_delete BEFORE DELETE ON customer_dues_ledger
BEGIN SELECT RAISE(ABORT, 'customer dues ledger cannot be deleted'); END;
"""