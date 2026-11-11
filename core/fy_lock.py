"""USR-05: lock a financial year after the books are closed.

  lock_year(conn, user_id, fin_year)            the owner closes a year, e.g. '25-26' (only a year that has ended)
  unlock_year(conn, user_id, fin_year, reason)  re-open it (reason required, in the audit log)
  locked_years(conn)                            the locked years
  ensure_open(conn, fin_year_or_date)           raises a clear message when that year is locked (used by billing,
                                                returns, purchases and expenses; the database triggers back it up)

A locked year stops anything NEW dated in it: back-dated bills, expenses, purchases. Today's work is dated today
(the current year), so a return of an old bill, a payment received now, or a stock count still work.
"""

import re
from datetime import date

from core.audit import log_event
from core.numbering import financial_year
from core.permissions import require_permission
from db.connection import transaction

_FY = re.compile(r"^(\d{2})-(\d{2})$")


def _label(value) -> str:
    """'25-26', or the year of a date / 'YYYY-MM-DD'."""
    if isinstance(value, (date, )) or (isinstance(value, str) and len(value) >= 10):
        return financial_year(value if isinstance(value, date) else value[:10])
    text = str(value or "").strip()
    m = _FY.match(text)
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise ValueError("A financial year looks like 25-26.")
    return text


def locked_years(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT fin_year, locked_at FROM locked_years ORDER BY fin_year")]


def is_locked(conn, value) -> bool:
    return conn.execute("SELECT 1 FROM locked_years WHERE fin_year = ?", (_label(value),)).fetchone() is not None


def ensure_open(conn, value) -> None:
    label = _label(value)
    if conn.execute("SELECT 1 FROM locked_years WHERE fin_year = ?", (label,)).fetchone():
        raise ValueError(f"The financial year {label} is locked. The owner must unlock it before anything is added to it.")


def lock_year(conn, user_id, fin_year, *, today=None) -> None:
    require_permission(conn, user_id, "manage_settings", "lock a financial year")
    label = _label(fin_year)
    if label == financial_year(today or date.today()) or int(label[:2]) > int(financial_year(today or date.today())[:2]):
        raise ValueError("Only a financial year that has ended can be locked.")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM locked_years WHERE fin_year = ?", (label,)).fetchone():
            raise ValueError(f"The financial year {label} is already locked.")
        conn.execute("INSERT INTO locked_years(fin_year, locked_by) VALUES (?,?)", (label, user_id))
        log_event(conn, "fin_year_locked", user_id=user_id, entity="fin_year", after={"fin_year": label})


def unlock_year(conn, user_id, fin_year, reason) -> None:
    require_permission(conn, user_id, "manage_settings", "unlock a financial year")
    label = _label(fin_year)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to unlock a financial year.")
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM locked_years WHERE fin_year = ?", (label,)).fetchone():
            raise ValueError(f"The financial year {label} is not locked.")
        conn.execute("DELETE FROM locked_years WHERE fin_year = ?", (label,))
        log_event(conn, "fin_year_unlocked", user_id=user_id, entity="fin_year", after={"fin_year": label, "reason": reason[:200]})
