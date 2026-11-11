# db/migration_14.py
"""Schema migration 14: stock held for a customer (SRS STK-03: the "on trial" and "reserved" buckets).

A hold takes pieces out of the sellable bucket for a few days, so billing cannot sell them:
  trial     the customer took the piece home to try it
  reserved  the piece is kept aside for a customer

stock_holds          one row per hold: what, how many, for whom, until when. Never edited.
stock_hold_releases  one row each time pieces come back to the shelf (all of a hold or part of it).
                     Still open = stock_holds.qty - the sum of its releases.

The pieces themselves move between buckets through stock_movements as usual (core/stock_holds.py does
both in one transaction). Both tables are append-only, like every other record of stock.
"""

MIGRATION_14 = """
CREATE TABLE stock_holds (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('trial','reserved')),
    variant_id INTEGER NOT NULL REFERENCES variants(id),
    qty        INTEGER NOT NULL CHECK (qty > 0),
    held_for   TEXT,
    note       TEXT,
    hold_until TEXT NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_stock_holds_variant ON stock_holds(variant_id);

CREATE TABLE stock_hold_releases (
    id         INTEGER PRIMARY KEY,
    hold_id    INTEGER NOT NULL REFERENCES stock_holds(id),
    qty        INTEGER NOT NULL CHECK (qty > 0),
    note       TEXT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_stock_hold_releases_hold ON stock_hold_releases(hold_id);

CREATE TRIGGER stock_holds_no_update BEFORE UPDATE ON stock_holds
BEGIN SELECT RAISE(ABORT, 'stock holds are append-only'); END;
CREATE TRIGGER stock_holds_no_delete BEFORE DELETE ON stock_holds
BEGIN SELECT RAISE(ABORT, 'stock holds are append-only'); END;
CREATE TRIGGER stock_hold_releases_no_update BEFORE UPDATE ON stock_hold_releases
BEGIN SELECT RAISE(ABORT, 'stock holds are append-only'); END;
CREATE TRIGGER stock_hold_releases_no_delete BEFORE DELETE ON stock_hold_releases
BEGIN SELECT RAISE(ABORT, 'stock holds are append-only'); END;
"""
