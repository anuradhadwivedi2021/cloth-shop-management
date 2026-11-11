# db/migration_16.py
"""Schema migration 16: estimates / quotations (SRS BIL-12).

An estimate is a price quote for a customer. It takes NO stock and makes NO sale. It can later be turned into a
real bill (which is a normal sale with its own invoice number); the estimate then points at that bill.

estimates       one row per estimate. Only an OPEN estimate can change status (to converted or cancelled);
                a closed one is final, and no row is ever deleted.
estimate_lines  what was quoted: item, pieces and the price then. Never edited or deleted.
"""

MIGRATION_16 = """
CREATE TABLE estimates (
    id           INTEGER PRIMARY KEY,
    number       INTEGER NOT NULL,
    estimate_no  TEXT NOT NULL UNIQUE,
    fin_year     TEXT NOT NULL,
    customer_id  INTEGER REFERENCES customers(id),
    customer_name TEXT,
    note         TEXT,
    valid_until  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','converted','cancelled')),
    sale_id      INTEGER REFERENCES sales(id),
    total_paise  INTEGER NOT NULL CHECK (total_paise > 0),
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    closed_at    TEXT
);
CREATE INDEX idx_estimates_status ON estimates(status, valid_until);
CREATE INDEX idx_estimates_customer ON estimates(customer_id);

CREATE TABLE estimate_lines (
    id                INTEGER PRIMARY KEY,
    estimate_id       INTEGER NOT NULL REFERENCES estimates(id),
    variant_id        INTEGER NOT NULL REFERENCES variants(id),
    label             TEXT NOT NULL,
    qty               INTEGER NOT NULL CHECK (qty > 0),
    unit_price_paise  INTEGER NOT NULL CHECK (unit_price_paise > 0),
    discount_paise    INTEGER NOT NULL DEFAULT 0 CHECK (discount_paise >= 0),
    line_total_paise  INTEGER NOT NULL CHECK (line_total_paise >= 0)
);
CREATE INDEX idx_estimate_lines_estimate ON estimate_lines(estimate_id);

CREATE TRIGGER estimates_no_delete BEFORE DELETE ON estimates
BEGIN SELECT RAISE(ABORT, 'Estimates cannot be deleted.'); END;
CREATE TRIGGER estimates_closed_is_final BEFORE UPDATE ON estimates WHEN OLD.status <> 'open'
BEGIN SELECT RAISE(ABORT, 'A closed estimate cannot be changed.'); END;
CREATE TRIGGER estimates_only_status_changes BEFORE UPDATE ON estimates
WHEN NEW.id <> OLD.id OR NEW.number <> OLD.number OR NEW.estimate_no <> OLD.estimate_no OR NEW.total_paise <> OLD.total_paise
  OR NEW.valid_until <> OLD.valid_until OR NEW.created_by <> OLD.created_by OR NEW.created_at <> OLD.created_at
  OR IFNULL(NEW.customer_id, 0) <> IFNULL(OLD.customer_id, 0)
BEGIN SELECT RAISE(ABORT, 'An estimate can only be closed, not edited.'); END;
CREATE TRIGGER estimate_lines_no_update BEFORE UPDATE ON estimate_lines
BEGIN SELECT RAISE(ABORT, 'Estimate lines cannot be changed.'); END;
CREATE TRIGGER estimate_lines_no_delete BEFORE DELETE ON estimate_lines
BEGIN SELECT RAISE(ABORT, 'Estimate lines cannot be deleted.'); END;
"""
