"""Schema migration 5 (Step 9): login lockout, owner PIN throttle, per-user permissions."""

MIGRATION_5 = """
ALTER TABLE users ADD COLUMN full_name TEXT;
ALTER TABLE users ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until TEXT;
ALTER TABLE users ADD COLUMN pin_failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN pin_locked_until TEXT;
ALTER TABLE users ADD COLUMN last_login_at TEXT;
-- NULL = use the role's default discount limit
ALTER TABLE users ADD COLUMN discount_cap_percent REAL
    CHECK (discount_cap_percent IS NULL OR (discount_cap_percent >= 0 AND discount_cap_percent <= 100));

-- Owner can grant or take away one permission for one user (on top of the role default).
CREATE TABLE user_permissions (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    permission TEXT NOT NULL,
    allowed    INTEGER NOT NULL CHECK (allowed IN (0,1)),
    set_by     INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (user_id, permission)
);
"""
