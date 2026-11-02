"""Schema migration 7 (Step 13): expenses and the cash register.

* expenses: every expense is one row that is never edited or deleted. A correction
  adds a linked reversal row (negative amount) and, if needed, a new corrected row,
  so the original entry, the reason, the time and the user are always preserved.
  Each row also has a matching row in `payments` (document_type 'expense') so the
  money paid out shows up in payment and cash reports.
* cash_days: one row per business day with opening cash, expected cash, the counted
  cash and the difference. A closed day can never be changed or deleted.
* cash_movements: cash put into or taken out of the drawer by hand (append-only).
"""

MIGRATION_7 = """
CREATE TABLE expenses (
    id              INTEGER PRIMARY KEY,
    expense_date    TEXT NOT NULL,
    category        TEXT NOT NULL,
    description     TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL CHECK (amount_paise != 0),
    payment_mode    TEXT NOT NULL CHECK (payment_mode IN ('cash','upi','card','bank_transfer','cheque','other')),
    reference       TEXT,
    notes           TEXT,
    attachment_path TEXT,
    reversal_of     INTEGER REFERENCES expenses(id),
    corrects_id     INTEGER REFERENCES expenses(id),
    reason          TEXT,
    user_id         INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK ((amount_paise < 0) = (reversal_of IS NOT NULL))
);
CREATE INDEX idx_expenses_date ON expenses(expense_date);
CREATE INDEX idx_expenses_category ON expenses(category);
CREATE UNIQUE INDEX idx_expenses_one_reversal ON expenses(reversal_of) WHERE reversal_of IS NOT NULL;

CREATE TRIGGER expenses_no_update BEFORE UPDATE ON expenses
BEGIN SELECT RAISE(ABORT, 'expenses cannot be edited; add a reversal or correction'); END;
CREATE TRIGGER expenses_no_delete BEFORE DELETE ON expenses
BEGIN SELECT RAISE(ABORT, 'expenses cannot be deleted; add a reversal'); END;

CREATE TABLE cash_days (
    id                INTEGER PRIMARY KEY,
    business_date     TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    opening_paise     INTEGER NOT NULL CHECK (opening_paise >= 0),
    opened_by         INTEGER REFERENCES users(id),
    opened_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expected_paise    INTEGER,
    counted_paise     INTEGER CHECK (counted_paise IS NULL OR counted_paise >= 0),
    difference_paise  INTEGER,
    difference_reason TEXT,
    closed_by         INTEGER REFERENCES users(id),
    closed_at         TEXT
);

CREATE TRIGGER cash_days_closed_is_final BEFORE UPDATE ON cash_days
WHEN OLD.status = 'closed'
BEGIN SELECT RAISE(ABORT, 'a closed cash day cannot be changed'); END;
CREATE TRIGGER cash_days_no_delete BEFORE DELETE ON cash_days
BEGIN SELECT RAISE(ABORT, 'cash days cannot be deleted'); END;

CREATE TABLE cash_movements (
    id            INTEGER PRIMARY KEY,
    business_date TEXT NOT NULL,
    amount_paise  INTEGER NOT NULL CHECK (amount_paise != 0),
    reason        TEXT NOT NULL,
    user_id       INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_cash_movements_date ON cash_movements(business_date);

CREATE TRIGGER cash_movements_no_update BEFORE UPDATE ON cash_movements
BEGIN SELECT RAISE(ABORT, 'cash movements are append-only'); END;
CREATE TRIGGER cash_movements_no_delete BEFORE DELETE ON cash_movements
BEGIN SELECT RAISE(ABORT, 'cash movements are append-only'); END;
"""
