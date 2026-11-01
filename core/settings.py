"""Shop settings stored in the database (key/value text).

Known keys used by the billing code:
  credit_sales_enabled  '1' to allow bills that are not fully paid (off by default)
  allow_negative_stock  '1' to let stock go below zero (off by default)
"""

from db.connection import transaction


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def get_bool(conn, key: str, default: bool = False) -> bool:
    value = get_setting(conn, key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def set_setting(conn, key: str, value) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("Setting name is required.")
    with transaction(conn):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
