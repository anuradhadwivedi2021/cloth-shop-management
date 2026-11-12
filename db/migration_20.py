# db/migration_20.py
"""Schema migration 20: commission rate per salesperson (SRS BIL-14).

commission_rates   a rate (in basis points: 100 = 1.00%) for one person. Someone without a row uses the shop's
                   default rate (Settings). Commission is worked out in the report from saved bills; nothing here
                   is money that was paid.
"""

MIGRATION_20 = """
CREATE TABLE commission_rates (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id),
    rate_bp    INTEGER NOT NULL CHECK (rate_bp BETWEEN 0 AND 5000),
    changed_by INTEGER REFERENCES users(id),
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
"""
