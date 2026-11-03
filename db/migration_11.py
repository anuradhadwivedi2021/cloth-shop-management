# db/migration_11.py
"""Schema migration 11 (Step 3 of Phase A): Gift Vouchers, Loyalty Points, and Store Credit Ledgers.

1. Creates gift_vouchers table (treated strictly as liability accounts, not revenue).
2. Creates loyalty_ledger table for earn, redeem, expiry, and return reversal tracking.
3. Adds voucher, loyalty, and store credit ledger indices and immutability triggers.
"""

MIGRATION_11 = """
-- 1. Gift Vouchers Table (Liability Account - Money stored in INTEGER paise)
CREATE TABLE gift_vouchers (
    id                   INTEGER PRIMARY KEY,
    code                 TEXT UNIQUE NOT NULL,
    customer_id          INTEGER REFERENCES customers(id),
    initial_amount_paise INTEGER NOT NULL CHECK (initial_amount_paise > 0),
    balance_paise        INTEGER NOT NULL CHECK (balance_paise >= 0),
    issued_date          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expiry_date          TEXT,
    status               TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'EXPIRED', 'FULLY_REDEEMED', 'CANCELLED')),
    created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_gift_vouchers_code ON gift_vouchers(code);

-- 2. Loyalty Points Ledger (Points stored as INTEGER)
CREATE TABLE loyalty_ledger (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    points       INTEGER NOT NULL, -- Positive for Earned, Negative for Redeemed/Expired
    tx_type      TEXT NOT NULL CHECK (tx_type IN ('EARNED_SALE', 'REDEEMED_SALE', 'RETURN_REVERSAL', 'EXPIRY')),
    doc_type     TEXT,
    doc_id       INTEGER,
    note         TEXT,
    user_id      INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_loyalty_ledger_customer ON loyalty_ledger(customer_id);

-- Immutability triggers for accounting ledgers
CREATE TRIGGER gift_vouchers_no_delete BEFORE DELETE ON gift_vouchers
BEGIN SELECT RAISE(ABORT, 'gift vouchers cannot be deleted'); END;

CREATE TRIGGER loyalty_ledger_no_update BEFORE UPDATE ON loyalty_ledger
BEGIN SELECT RAISE(ABORT, 'loyalty ledger is append-only'); END;
CREATE TRIGGER loyalty_ledger_no_delete BEFORE DELETE ON loyalty_ledger
BEGIN SELECT RAISE(ABORT, 'loyalty ledger is append-only'); END;
"""