"""Schema migration 9 (Step 15): the shop profile, kept as a history.

Every time the owner changes the shop name, address, GSTIN, state, tax scheme or UPI id,
a NEW row is added here. Old rows are never edited or deleted, so an old bill can always be
reprinted with the shop details that were valid on its date (a GST invoice must show the
GSTIN it was issued under).
"""

MIGRATION_9 = """
CREATE TABLE shop_profile_versions (
    id          INTEGER PRIMARY KEY,
    valid_from  TEXT NOT NULL,
    name        TEXT NOT NULL,
    address     TEXT,
    phone       TEXT,
    email       TEXT,
    gstin       TEXT,
    state_code  TEXT,
    tax_scheme  TEXT NOT NULL DEFAULT 'regular' CHECK (tax_scheme IN ('regular','composition','unregistered')),
    upi_id      TEXT,
    user_id     INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_shop_profile_valid_from ON shop_profile_versions(valid_from);

CREATE TRIGGER shop_profile_no_update BEFORE UPDATE ON shop_profile_versions
BEGIN SELECT RAISE(ABORT, 'shop profile history cannot be edited; a change adds a new version'); END;
CREATE TRIGGER shop_profile_no_delete BEFORE DELETE ON shop_profile_versions
BEGIN SELECT RAISE(ABORT, 'shop profile history cannot be deleted'); END;
"""