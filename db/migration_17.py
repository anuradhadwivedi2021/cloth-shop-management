# db/migration_17.py
"""Schema migration 17: movements of gift vouchers (SRS CUS-05, CUS-06).

Loyalty points (loyalty_ledger), gift vouchers (gift_vouchers) and store credit (store_credit_ledger) already exist
from earlier migrations. This adds:

voucher_ledger   +value when a voucher is issued, -amount when it is used on a bill. Append-only. The voucher's
                 balance_paise always equals the sum of its entries (the code keeps both in one transaction).
and rules that keep a voucher's code, value, customer and issue date from ever being changed.
"""

MIGRATION_17 = """
CREATE TABLE voucher_ledger (
    id           INTEGER PRIMARY KEY,
    voucher_id   INTEGER NOT NULL REFERENCES gift_vouchers(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise != 0),
    doc_type     TEXT,
    doc_id       INTEGER,
    user_id      INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX idx_voucher_ledger_voucher ON voucher_ledger(voucher_id);
CREATE TRIGGER voucher_ledger_no_update BEFORE UPDATE ON voucher_ledger
BEGIN SELECT RAISE(ABORT, 'voucher ledger is append-only'); END;
CREATE TRIGGER voucher_ledger_no_delete BEFORE DELETE ON voucher_ledger
BEGIN SELECT RAISE(ABORT, 'voucher ledger is append-only'); END;
CREATE TRIGGER gift_vouchers_fixed_fields BEFORE UPDATE ON gift_vouchers
WHEN NEW.id <> OLD.id OR NEW.code <> OLD.code OR NEW.initial_amount_paise <> OLD.initial_amount_paise
  OR COALESCE(NEW.customer_id, 0) <> COALESCE(OLD.customer_id, 0) OR COALESCE(NEW.expiry_date, '') <> COALESCE(OLD.expiry_date, '')
  OR NEW.issued_date <> OLD.issued_date
BEGIN SELECT RAISE(ABORT, 'only the balance and status of a voucher can change'); END;
"""
