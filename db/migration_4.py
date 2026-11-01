"""Schema migration 4 (Step 8): stock counts, stock adjustments, purchase returns.

Added as its own file; db/schema.py only imports it and registers it in MIGRATIONS.
"""

MIGRATION_4 = """
-- Damaged / missing stock and the check of returned pieces. One row per action.
CREATE TABLE stock_adjustments (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('damaged','missing','returned_ok','returned_damaged')),
    variant_id INTEGER NOT NULL REFERENCES variants(id),
    qty        INTEGER NOT NULL CHECK (qty > 0),
    reason     TEXT NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_stock_adjustments_variant ON stock_adjustments(variant_id);

-- Physical stock count: expected quantity is frozen when the count starts.
CREATE TABLE stock_counts (
    id          INTEGER PRIMARY KEY,
    count_no    TEXT NOT NULL UNIQUE,
    fin_year    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','applied','cancelled')),
    blind       INTEGER NOT NULL DEFAULT 0 CHECK (blind IN (0,1)),
    scope       TEXT,
    note        TEXT,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    approved_by INTEGER REFERENCES users(id),
    closed_at   TEXT,
    close_reason TEXT
);

CREATE TABLE stock_count_lines (
    id           INTEGER PRIMARY KEY,
    count_id     INTEGER NOT NULL REFERENCES stock_counts(id),
    variant_id   INTEGER NOT NULL REFERENCES variants(id),
    expected_qty INTEGER NOT NULL,
    counted_qty  INTEGER CHECK (counted_qty IS NULL OR counted_qty >= 0),
    reason       TEXT,
    counted_by   INTEGER REFERENCES users(id),
    counted_at   TEXT,
    UNIQUE (count_id, variant_id)
);
CREATE INDEX idx_stock_count_lines_count ON stock_count_lines(count_id);

-- Debit notes: goods sent back to a supplier against a confirmed purchase.
-- Rule: total_paise = due_reduced_paise + refund_paise + supplier_credit_paise.
CREATE TABLE purchase_returns (
    id                   INTEGER PRIMARY KEY,
    series               TEXT NOT NULL,
    number               INTEGER NOT NULL,
    debit_note_no        TEXT NOT NULL UNIQUE,
    fin_year             TEXT NOT NULL,
    purchase_id          INTEGER NOT NULL REFERENCES purchases(id),
    supplier_id          INTEGER NOT NULL REFERENCES suppliers(id),
    user_id              INTEGER NOT NULL REFERENCES users(id),
    reason               TEXT NOT NULL,
    subtotal_paise       INTEGER NOT NULL,
    discount_paise       INTEGER NOT NULL DEFAULT 0,
    tax_paise            INTEGER NOT NULL DEFAULT 0,
    round_off_paise      INTEGER NOT NULL DEFAULT 0,
    total_paise          INTEGER NOT NULL CHECK (total_paise >= 0),
    due_reduced_paise    INTEGER NOT NULL DEFAULT 0,
    refund_paise         INTEGER NOT NULL DEFAULT 0,
    supplier_credit_paise INTEGER NOT NULL DEFAULT 0,
    idempotency_key      TEXT UNIQUE,
    created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (series, fin_year, number)
);
CREATE INDEX idx_purchase_returns_purchase ON purchase_returns(purchase_id);

CREATE TABLE purchase_return_lines (
    id               INTEGER PRIMARY KEY,
    return_id        INTEGER NOT NULL REFERENCES purchase_returns(id),
    purchase_line_id INTEGER NOT NULL REFERENCES purchase_lines(id),
    variant_id       INTEGER NOT NULL REFERENCES variants(id),
    qty              INTEGER NOT NULL CHECK (qty > 0),
    unit_cost_paise  INTEGER NOT NULL,
    tax_paise        INTEGER NOT NULL DEFAULT 0,
    line_total_paise INTEGER NOT NULL,
    reason           TEXT NOT NULL
);
CREATE INDEX idx_purchase_return_lines_return ON purchase_return_lines(return_id);
CREATE INDEX idx_purchase_return_lines_pline ON purchase_return_lines(purchase_line_id);

-- Append-only / immutable rules
CREATE TRIGGER stock_adjustments_no_update BEFORE UPDATE ON stock_adjustments
BEGIN SELECT RAISE(ABORT, 'stock adjustments are append-only'); END;
CREATE TRIGGER stock_adjustments_no_delete BEFORE DELETE ON stock_adjustments
BEGIN SELECT RAISE(ABORT, 'stock adjustments are append-only'); END;

CREATE TRIGGER stock_counts_no_delete BEFORE DELETE ON stock_counts
BEGIN SELECT RAISE(ABORT, 'stock counts cannot be deleted'); END;
CREATE TRIGGER stock_counts_closed_no_update BEFORE UPDATE ON stock_counts
WHEN OLD.status != 'open'
BEGIN SELECT RAISE(ABORT, 'a closed stock count cannot be changed'); END;
CREATE TRIGGER stock_count_lines_no_delete BEFORE DELETE ON stock_count_lines
BEGIN SELECT RAISE(ABORT, 'stock count lines cannot be deleted'); END;
CREATE TRIGGER stock_count_lines_closed_no_update BEFORE UPDATE ON stock_count_lines
WHEN (SELECT status FROM stock_counts WHERE id = OLD.count_id) != 'open'
BEGIN SELECT RAISE(ABORT, 'a closed stock count cannot be changed'); END;

CREATE TRIGGER purchase_returns_no_update BEFORE UPDATE ON purchase_returns
BEGIN SELECT RAISE(ABORT, 'debit notes are immutable'); END;
CREATE TRIGGER purchase_returns_no_delete BEFORE DELETE ON purchase_returns
BEGIN SELECT RAISE(ABORT, 'debit notes cannot be deleted'); END;
CREATE TRIGGER purchase_return_lines_no_update BEFORE UPDATE ON purchase_return_lines
BEGIN SELECT RAISE(ABORT, 'debit note lines are immutable'); END;
CREATE TRIGGER purchase_return_lines_no_delete BEFORE DELETE ON purchase_return_lines
BEGIN SELECT RAISE(ABORT, 'debit note lines cannot be deleted'); END;
"""
