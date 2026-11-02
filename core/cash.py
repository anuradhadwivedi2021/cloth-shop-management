"""Cash register: open the day, record cash put in or taken out, close the day.

Expected cash is worked out from saved records only:

    opening cash
    + cash received on bills
    - cash refunded to customers
    - cash paid to suppliers on purchases
    + cash refunded by suppliers on purchase returns
    - cash paid for expenses
    + cash put into the drawer by hand
    - cash taken out of the drawer by hand

At closing the owner enters the counted cash. The difference is SHOWN (with a reason),
never used to change any figure. A closed day cannot be edited. If a cash payment is
dated into a day after it was closed, `get_cash_day` flags it instead of silently
changing the closed figures.

Sign convention in the `payments` table: bills and customer refunds are stored as seen
from the shop's till (money in is positive, a refund is negative); purchases, purchase
returns and expenses are stored as seen from the supplier or expense side (a payment
OUT is positive, a refund IN is negative). `_CASH_SIGN` turns both into "cash in the drawer".

Needs the "manage_cash" permission. Reading also allows "view_reports".
"""

from datetime import date

from core.audit import log_event
from core.permissions import PermissionDenied, has_permission, require_permission
from db.connection import transaction

# document_type -> effect on cash in the drawer (multiplied by the stored amount)
_CASH_SIGN = {
    "sale": 1,
    "sale_return": 1,
    "purchase": -1,
    "purchase_return": -1,
    "expense": -1,
}


def _iso_date(value) -> str:
    text = value.isoformat() if isinstance(value, date) else str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("Date must be a valid date (YYYY-MM-DD).") from None


def _cash_by_type(conn, business_date: str) -> dict:
    rows = conn.execute(
        "SELECT document_type, SUM(amount_paise) AS total FROM payments "
        "WHERE mode = 'cash' AND substr(created_at, 1, 10) = ? "
        "AND document_type IN ('sale','sale_return','purchase','purchase_return','expense') "
        "GROUP BY document_type",
        (business_date,),
    ).fetchall()
    return {r["document_type"]: r["total"] for r in rows}


def _movement_totals(conn, business_date: str):
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN amount_paise > 0 THEN amount_paise END), 0) AS money_in, "
        "COALESCE(-SUM(CASE WHEN amount_paise < 0 THEN amount_paise END), 0) AS money_out "
        "FROM cash_movements WHERE business_date = ?",
        (business_date,),
    ).fetchone()
    return row["money_in"], row["money_out"]


def _figures(conn, business_date: str, opening_paise: int) -> dict:
    by_type = _cash_by_type(conn, business_date)
    manual_in, manual_out = _movement_totals(conn, business_date)
    figures = {
        "opening_paise": opening_paise,
        "cash_sales_paise": by_type.get("sale", 0),
        "customer_refunds_paise": -by_type.get("sale_return", 0),
        "supplier_payments_paise": by_type.get("purchase", 0),
        "supplier_refunds_paise": -by_type.get("purchase_return", 0),
        "expenses_paise": by_type.get("expense", 0),
        "manual_in_paise": manual_in,
        "manual_out_paise": manual_out,
    }
    figures["expected_paise"] = (
        opening_paise
        + figures["cash_sales_paise"]
        - figures["customer_refunds_paise"]
        - figures["supplier_payments_paise"]
        + figures["supplier_refunds_paise"]
        - figures["expenses_paise"]
        + manual_in
        - manual_out
    )
    return figures


def _day_row(conn, business_date: str):
    return conn.execute("SELECT * FROM cash_days WHERE business_date = ?", (business_date,)).fetchone()


# ---------- actions ----------

def open_cash_day(conn, business_date, *, user_id, opening_paise=None) -> int:
    """Open the cash register for a day. Returns the cash day id.

    Without `opening_paise`, the opening cash is the cash counted when the last day was closed.
    Only one day can be open at a time.
    """
    require_permission(conn, user_id, "manage_cash", "open the cash register")
    day = _iso_date(business_date)
    if opening_paise is not None and (
        not isinstance(opening_paise, int) or isinstance(opening_paise, bool) or opening_paise < 0
    ):
        raise ValueError("Opening cash must be zero or more.")

    with transaction(conn):
        if _day_row(conn, day):
            raise ValueError(f"The cash register for {day} has already been opened.")
        still_open = conn.execute("SELECT business_date FROM cash_days WHERE status = 'open'").fetchone()
        if still_open:
            raise ValueError(f"Close the cash day of {still_open['business_date']} before opening a new one.")

        previous = conn.execute(
            "SELECT business_date, counted_paise FROM cash_days WHERE status = 'closed' AND business_date < ? "
            "ORDER BY business_date DESC LIMIT 1",
            (day,),
        ).fetchone()
        carried = previous["counted_paise"] if previous else 0
        opening = carried if opening_paise is None else opening_paise

        cur = conn.execute(
            "INSERT INTO cash_days(business_date, opening_paise, opened_by) VALUES (?,?,?)",
            (day, opening, user_id),
        )
        log_event(
            conn, "cash_day_opened", user_id=user_id, entity="cash_day", entity_id=cur.lastrowid,
            after={"date": day, "opening_paise": opening, "carried_from_last_close_paise": carried},
        )
        return cur.lastrowid


def add_cash_movement(conn, business_date, amount_paise, reason, *, user_id) -> int:
    """Put cash into (positive) or take cash out of (negative) the drawer by hand. Returns its id."""
    require_permission(conn, user_id, "manage_cash", "record cash movements")
    day = _iso_date(business_date)
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool) or amount_paise == 0:
        raise ValueError("Amount must be a non-zero amount (positive = cash in, negative = cash out).")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required.")

    with transaction(conn):
        row = _day_row(conn, day)
        if row is None or row["status"] != "open":
            raise ValueError("The cash register for this day is not open.")
        if _figures(conn, day, row["opening_paise"])["expected_paise"] + amount_paise < 0:
            raise ValueError("There is not enough cash in the drawer for this.")
        cur = conn.execute(
            "INSERT INTO cash_movements(business_date, amount_paise, reason, user_id) VALUES (?,?,?,?)",
            (day, amount_paise, reason, user_id),
        )
        log_event(
            conn, "cash_movement", user_id=user_id, entity="cash_movement", entity_id=cur.lastrowid,
            after={"date": day, "amount_paise": amount_paise, "reason": reason},
        )
        return cur.lastrowid


def close_cash_day(conn, business_date, counted_paise, *, user_id, reason=None) -> dict:
    """Close the day by comparing the counted cash with the expected cash.

    A difference needs a reason. The figures are never changed to make them match.
    Returns the closing figures.
    """
    require_permission(conn, user_id, "manage_cash", "close the cash register")
    day = _iso_date(business_date)
    if not isinstance(counted_paise, int) or isinstance(counted_paise, bool) or counted_paise < 0:
        raise ValueError("Counted cash must be zero or more.")

    with transaction(conn):
        row = _day_row(conn, day)
        if row is None:
            raise ValueError("The cash register for this day was never opened.")
        if row["status"] != "open":
            raise ValueError("This cash day is already closed.")
        figures = _figures(conn, day, row["opening_paise"])
        difference = counted_paise - figures["expected_paise"]
        reason = (reason or "").strip() or None
        if difference != 0 and reason is None:
            raise ValueError(
                f"Counted cash differs from expected cash by {difference} paise. Enter a reason to close the day."
            )
        conn.execute(
            "UPDATE cash_days SET status = 'closed', expected_paise = ?, counted_paise = ?, "
            "difference_paise = ?, difference_reason = ?, closed_by = ?, closed_at = datetime('now','localtime') "
            "WHERE id = ?",
            (figures["expected_paise"], counted_paise, difference, reason, user_id, row["id"]),
        )
        log_event(
            conn, "cash_day_closed", user_id=user_id, entity="cash_day", entity_id=row["id"],
            after={
                "date": day, "expected_paise": figures["expected_paise"],
                "counted_paise": counted_paise, "difference_paise": difference, "reason": reason,
            },
        )
        return {**figures, "business_date": day, "counted_paise": counted_paise,
                "difference_paise": difference, "difference_reason": reason}


# ---------- reading ----------

def get_cash_day(conn, business_date, *, user_id) -> dict:
    """The day's cash figures from saved records.

    For a closed day, `changed_after_close` is True when cash payments dated into that day were
    saved after closing (the closed figures are left as they were).
    """
    if not (has_permission(conn, user_id, "manage_cash") or has_permission(conn, user_id, "view_reports")):
        raise PermissionDenied("You are not allowed to view the cash register.")
    day = _iso_date(business_date)
    row = _day_row(conn, day)
    opening = row["opening_paise"] if row else 0
    figures = _figures(conn, day, opening)
    result = {"business_date": day, "status": row["status"] if row else "not_opened", **figures}
    if row and row["status"] == "closed":
        result.update(
            counted_paise=row["counted_paise"],
            difference_paise=row["difference_paise"],
            difference_reason=row["difference_reason"],
            closed_expected_paise=row["expected_paise"],
            changed_after_close=figures["expected_paise"] != row["expected_paise"],
        )
    return result
