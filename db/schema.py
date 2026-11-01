"""Database schema and migrations.

All money columns are INTEGER paise (Rs 1,050 = 105000) so totals are exact.
New schema changes must be added as a new entry in MIGRATIONS, never by
editing an older one.
"""

from db.migration_4 import MIGRATION_4

MIGRATION_1 = """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    pin_hash      TEXT,
    role          TEXT NOT NULL CHECK (role IN ('owner','cashier','stock','accountant')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    entity      TEXT,
    entity_id   INTEGER,
    before_json TEXT,
    after_json  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    prev_hash   TEXT,
    hash        TEXT
);

CREATE TABLE products (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    category            TEXT,
    brand               TEXT,
    hsn                 TEXT,
    material            TEXT,
    season              TEXT,
    description         TEXT,
    gst_rate_percent    REAL,
    mrp_paise           INTEGER CHECK (mrp_paise IS NULL OR mrp_paise >= 0),
    default_cost_paise  INTEGER NOT NULL DEFAULT 0 CHECK (default_cost_paise >= 0),
    default_price_paise INTEGER NOT NULL DEFAULT 0 CHECK (default_price_paise >= 0),
    unit                TEXT NOT NULL DEFAULT 'piece' CHECK (unit IN ('piece','pair','set','metre')),
    reorder_level       INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_products_name ON products(name);

CREATE TABLE variants (
    id                   INTEGER PRIMARY KEY,
    product_id           INTEGER NOT NULL REFERENCES products(id),
    size                 TEXT NOT NULL,
    colour               TEXT NOT NULL,
    sku                  TEXT NOT NULL UNIQUE,
    barcode              TEXT UNIQUE,
    alt_barcode          TEXT,
    price_override_paise INTEGER CHECK (price_override_paise IS NULL OR price_override_paise >= 0),
    reorder_level        INTEGER,
    rack_location        TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (product_id, size, colour)
);
CREATE INDEX idx_variants_product ON variants(product_id);
CREATE INDEX idx_variants_alt_barcode ON variants(alt_barcode);

CREATE TABLE suppliers (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    contact_person TEXT,
    phone          TEXT,
    email          TEXT,
    address        TEXT,
    gstin          TEXT,
    state          TEXT,
    opening_balance_paise INTEGER NOT NULL DEFAULT 0,
    notes          TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE customers (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    phone           TEXT NOT NULL,
    address         TEXT,
    gstin           TEXT,
    birthday        TEXT,
    preferred_size  TEXT,
    notes           TEXT,
    consent_given   INTEGER NOT NULL DEFAULT 0 CHECK (consent_given IN (0,1)),
    consent_date    TEXT,
    consent_purpose TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_customers_phone ON customers(phone);

CREATE TABLE tax_rules (
    id                  INTEGER PRIMARY KEY,
    hsn_prefix          TEXT NOT NULL,
    rate_percent        REAL NOT NULL CHECK (rate_percent >= 0),
    price_ceiling_paise INTEGER,
    valid_from          TEXT NOT NULL,
    valid_to            TEXT,
    note                TEXT
);

CREATE TABLE number_series (
    series      TEXT NOT NULL,
    fin_year    TEXT NOT NULL,
    prefix      TEXT NOT NULL,
    last_number INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (series, fin_year)
);

CREATE TABLE purchases (
    id               INTEGER PRIMARY KEY,
    supplier_id      INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_no       TEXT NOT NULL,
    invoice_date     TEXT NOT NULL,
    received_date    TEXT,
    fin_year         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed','cancelled')),
    voucher_no       TEXT UNIQUE,
    subtotal_paise   INTEGER NOT NULL DEFAULT 0,
    discount_paise   INTEGER NOT NULL DEFAULT 0,
    tax_paise        INTEGER NOT NULL DEFAULT 0,
    freight_paise    INTEGER NOT NULL DEFAULT 0,
    round_off_paise  INTEGER NOT NULL DEFAULT 0,
    total_paise      INTEGER NOT NULL DEFAULT 0,
    paid_paise       INTEGER NOT NULL DEFAULT 0,
    due_paise        INTEGER NOT NULL DEFAULT 0,
    notes            TEXT,
    created_by       INTEGER REFERENCES users(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    confirmed_at     TEXT
);
CREATE INDEX idx_purchases_supplier_invoice ON purchases(supplier_id, invoice_no, fin_year);

CREATE TABLE purchase_lines (
    id               INTEGER PRIMARY KEY,
    purchase_id      INTEGER NOT NULL REFERENCES purchases(id),
    variant_id       INTEGER NOT NULL REFERENCES variants(id),
    qty              INTEGER NOT NULL CHECK (qty > 0),
    unit_cost_paise  INTEGER NOT NULL CHECK (unit_cost_paise >= 0),
    tax_rate_percent REAL NOT NULL DEFAULT 0,
    tax_paise        INTEGER NOT NULL DEFAULT 0,
    line_total_paise INTEGER NOT NULL
);
CREATE INDEX idx_purchase_lines_purchase ON purchase_lines(purchase_id);

CREATE TABLE sales (
    id               INTEGER PRIMARY KEY,
    series           TEXT NOT NULL,
    number           INTEGER NOT NULL,
    invoice_no       TEXT NOT NULL UNIQUE,
    fin_year         TEXT NOT NULL,
    customer_id      INTEGER REFERENCES customers(id),
    salesperson_id   INTEGER REFERENCES users(id),
    user_id          INTEGER NOT NULL REFERENCES users(id),
    tax_mode         TEXT NOT NULL CHECK (tax_mode IN ('regular','composition','unregistered')),
    status           TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed','cancelled')),
    subtotal_paise   INTEGER NOT NULL,
    discount_paise   INTEGER NOT NULL DEFAULT 0,
    taxable_paise    INTEGER NOT NULL,
    tax_paise        INTEGER NOT NULL DEFAULT 0,
    round_off_paise  INTEGER NOT NULL DEFAULT 0,
    total_paise      INTEGER NOT NULL,
    idempotency_key  TEXT UNIQUE,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (series, fin_year, number)
);
CREATE INDEX idx_sales_created ON sales(created_at);
CREATE INDEX idx_sales_customer ON sales(customer_id);

CREATE TABLE sale_lines (
    id                INTEGER PRIMARY KEY,
    sale_id           INTEGER NOT NULL REFERENCES sales(id),
    variant_id        INTEGER NOT NULL REFERENCES variants(id),
    product_name      TEXT NOT NULL,
    size              TEXT NOT NULL,
    colour            TEXT NOT NULL,
    hsn               TEXT,
    qty               INTEGER NOT NULL CHECK (qty > 0),
    unit_price_paise  INTEGER NOT NULL CHECK (unit_price_paise >= 0),
    discount_paise    INTEGER NOT NULL DEFAULT 0,
    taxable_paise     INTEGER NOT NULL,
    tax_rate_percent  REAL NOT NULL DEFAULT 0,
    tax_paise         INTEGER NOT NULL DEFAULT 0,
    line_total_paise  INTEGER NOT NULL,
    tax_rule_id       INTEGER REFERENCES tax_rules(id)
);
CREATE INDEX idx_sale_lines_sale ON sale_lines(sale_id);
CREATE INDEX idx_sale_lines_variant ON sale_lines(variant_id);

CREATE TABLE payments (
    id             INTEGER PRIMARY KEY,
    document_type  TEXT NOT NULL CHECK (document_type IN ('sale','purchase','sale_return','purchase_return','expense','other')),
    document_id    INTEGER,
    mode           TEXT NOT NULL CHECK (mode IN ('cash','upi','card','bank_transfer','cheque','store_credit','voucher','loyalty','other')),
    amount_paise   INTEGER NOT NULL CHECK (amount_paise != 0),
    reference      TEXT,
    reversal_of    INTEGER REFERENCES payments(id),
    reason         TEXT,
    user_id        INTEGER REFERENCES users(id),
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_payments_doc ON payments(document_type, document_id);

CREATE TABLE stock_movements (
    id            INTEGER PRIMARY KEY,
    variant_id    INTEGER NOT NULL REFERENCES variants(id),
    qty_change    INTEGER NOT NULL CHECK (qty_change != 0),
    bucket        TEXT NOT NULL DEFAULT 'sellable'
                  CHECK (bucket IN ('sellable','damaged','returned_pending','on_trial','reserved')),
    reason        TEXT NOT NULL,
    doc_type      TEXT,
    doc_id        INTEGER,
    user_id       INTEGER REFERENCES users(id),
    balance_after INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_stock_movements_variant ON stock_movements(variant_id, bucket);

CREATE VIEW variant_stock AS
SELECT v.id AS variant_id,
       COALESCE(SUM(CASE WHEN m.bucket = 'sellable'         THEN m.qty_change END), 0) AS sellable_qty,
       COALESCE(SUM(CASE WHEN m.bucket = 'damaged'          THEN m.qty_change END), 0) AS damaged_qty,
       COALESCE(SUM(CASE WHEN m.bucket = 'returned_pending' THEN m.qty_change END), 0) AS returned_pending_qty,
       COALESCE(SUM(CASE WHEN m.bucket = 'on_trial'         THEN m.qty_change END), 0) AS on_trial_qty,
       COALESCE(SUM(CASE WHEN m.bucket = 'reserved'         THEN m.qty_change END), 0) AS reserved_qty
FROM variants v
LEFT JOIN stock_movements m ON m.variant_id = v.id
GROUP BY v.id;

-- Immutability rules (SRS: no silent edits, append-only records)
CREATE TRIGGER stock_movements_no_update BEFORE UPDATE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only'); END;
CREATE TRIGGER stock_movements_no_delete BEFORE DELETE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only'); END;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER payments_no_update BEFORE UPDATE ON payments
BEGIN SELECT RAISE(ABORT, 'payments cannot be edited; add a reversal entry'); END;
CREATE TRIGGER payments_no_delete BEFORE DELETE ON payments
BEGIN SELECT RAISE(ABORT, 'payments cannot be deleted; add a reversal entry'); END;

CREATE TRIGGER sale_lines_no_update BEFORE UPDATE ON sale_lines
BEGIN SELECT RAISE(ABORT, 'saved bill lines are immutable'); END;
CREATE TRIGGER sale_lines_no_delete BEFORE DELETE ON sale_lines
BEGIN SELECT RAISE(ABORT, 'saved bill lines are immutable'); END;

CREATE TRIGGER sales_no_delete BEFORE DELETE ON sales
BEGIN SELECT RAISE(ABORT, 'saved bills cannot be deleted; cancel or issue a credit note'); END;
CREATE TRIGGER sales_no_edit BEFORE UPDATE OF
    series, number, invoice_no, fin_year, customer_id, salesperson_id, user_id, tax_mode,
    subtotal_paise, discount_paise, taxable_paise, tax_paise, round_off_paise,
    total_paise, idempotency_key, created_at
ON sales
BEGIN SELECT RAISE(ABORT, 'saved bills are immutable'); END;

CREATE TRIGGER purchases_no_delete_confirmed BEFORE DELETE ON purchases
WHEN OLD.status != 'draft'
BEGIN SELECT RAISE(ABORT, 'confirmed purchases cannot be deleted; cancel or return instead'); END;

CREATE TRIGGER purchase_lines_no_update_confirmed BEFORE UPDATE ON purchase_lines
WHEN (SELECT status FROM purchases WHERE id = OLD.purchase_id) != 'draft'
BEGIN SELECT RAISE(ABORT, 'confirmed purchase lines are immutable'); END;
CREATE TRIGGER purchase_lines_no_delete_confirmed BEFORE DELETE ON purchase_lines
WHEN (SELECT status FROM purchases WHERE id = OLD.purchase_id) != 'draft'
BEGIN SELECT RAISE(ABORT, 'confirmed purchase lines are immutable'); END;
"""

MIGRATION_2 = """
-- Sales: facts the receipt and tax split need, kept with the saved bill
ALTER TABLE sales ADD COLUMN prices_include_tax INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sales ADD COLUMN intra_state        INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sales ADD COLUMN calc_method        TEXT;
ALTER TABLE sales ADD COLUMN tendered_paise     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sales ADD COLUMN change_paise       INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER sales_no_edit_2 BEFORE UPDATE OF
    prices_include_tax, intra_state, calc_method, tendered_paise, change_paise
ON sales
BEGIN SELECT RAISE(ABORT, 'saved bills are immutable'); END;

-- Bills parked at the counter (max 10 at once, one slot each)
CREATE TABLE held_bills (
    slot        INTEGER PRIMARY KEY CHECK (slot BETWEEN 1 AND 10),
    user_id     INTEGER REFERENCES users(id),
    customer_id INTEGER REFERENCES customers(id),
    note        TEXT,
    cart_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

MIGRATION_3 = """
-- Credit notes for sales returns and exchanges. Each one is linked to the
-- original bill (sale_id) and, for an exchange, to the replacement bill.
-- Rule: total_paise = due_adjusted + refund + store_credit + credit_applied.
CREATE TABLE sale_returns (
    id                  INTEGER PRIMARY KEY,
    series              TEXT NOT NULL,
    number              INTEGER NOT NULL,
    credit_note_no      TEXT NOT NULL UNIQUE,
    fin_year            TEXT NOT NULL,
    sale_id             INTEGER NOT NULL REFERENCES sales(id),
    customer_id         INTEGER REFERENCES customers(id),
    user_id             INTEGER NOT NULL REFERENCES users(id),
    outcome             TEXT NOT NULL CHECK (outcome IN ('refund','store_credit','exchange')),
    reason              TEXT NOT NULL,
    taxable_paise       INTEGER NOT NULL,
    tax_paise           INTEGER NOT NULL DEFAULT 0,
    round_off_paise     INTEGER NOT NULL DEFAULT 0,
    total_paise         INTEGER NOT NULL CHECK (total_paise >= 0),
    due_adjusted_paise  INTEGER NOT NULL DEFAULT 0,
    refund_paise        INTEGER NOT NULL DEFAULT 0,
    store_credit_paise  INTEGER NOT NULL DEFAULT 0,
    credit_applied_paise INTEGER NOT NULL DEFAULT 0,
    exchange_sale_id    INTEGER REFERENCES sales(id),
    override_user_id    INTEGER REFERENCES users(id),
    override_reason     TEXT,
    idempotency_key     TEXT UNIQUE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (series, fin_year, number)
);
CREATE INDEX idx_sale_returns_sale ON sale_returns(sale_id);

CREATE TABLE sale_return_lines (
    id               INTEGER PRIMARY KEY,
    return_id        INTEGER NOT NULL REFERENCES sale_returns(id),
    sale_line_id     INTEGER NOT NULL REFERENCES sale_lines(id),
    variant_id       INTEGER NOT NULL REFERENCES variants(id),
    product_name     TEXT NOT NULL,
    size             TEXT NOT NULL,
    colour           TEXT NOT NULL,
    qty              INTEGER NOT NULL CHECK (qty > 0),
    taxable_paise    INTEGER NOT NULL,
    tax_paise        INTEGER NOT NULL DEFAULT 0,
    line_total_paise INTEGER NOT NULL,
    reason           TEXT NOT NULL
);
CREATE INDEX idx_sale_return_lines_return ON sale_return_lines(return_id);
CREATE INDEX idx_sale_return_lines_sale_line ON sale_return_lines(sale_line_id);

-- Store credit given to a customer (issuing only; spending it is Phase 2).
-- Balance = sum of amounts. Append-only.
CREATE TABLE store_credit_ledger (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise != 0),
    doc_type     TEXT,
    doc_id       INTEGER,
    note         TEXT,
    user_id      INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_store_credit_customer ON store_credit_ledger(customer_id);

CREATE TRIGGER sale_returns_no_update BEFORE UPDATE ON sale_returns
BEGIN SELECT RAISE(ABORT, 'credit notes are immutable'); END;
CREATE TRIGGER sale_returns_no_delete BEFORE DELETE ON sale_returns
BEGIN SELECT RAISE(ABORT, 'credit notes cannot be deleted'); END;
CREATE TRIGGER sale_return_lines_no_update BEFORE UPDATE ON sale_return_lines
BEGIN SELECT RAISE(ABORT, 'credit note lines are immutable'); END;
CREATE TRIGGER sale_return_lines_no_delete BEFORE DELETE ON sale_return_lines
BEGIN SELECT RAISE(ABORT, 'credit note lines cannot be deleted'); END;
CREATE TRIGGER store_credit_ledger_no_update BEFORE UPDATE ON store_credit_ledger
BEGIN SELECT RAISE(ABORT, 'store credit ledger is append-only'); END;
CREATE TRIGGER store_credit_ledger_no_delete BEFORE DELETE ON store_credit_ledger
BEGIN SELECT RAISE(ABORT, 'store credit ledger is append-only'); END;
"""

MIGRATIONS = {
    1: MIGRATION_1,
    2: MIGRATION_2,
    3: MIGRATION_3,
    4: MIGRATION_4,
}


def migrate(conn):
    """Bring the database up to the latest schema version (safe to call every start)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))"
    )
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]

    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        script = (
            "BEGIN;\n"
            + MIGRATIONS[version]
            + f"\nINSERT INTO schema_version(version) VALUES ({version});\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise