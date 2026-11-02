"""Shop expenses (rent, electricity, salaries, bags, ...).

Rules (SRS section 15 and PAY-03/PAY-04):
* An expense is never edited or deleted. A mistake is fixed with `reverse_expense`
  (adds a linked negative entry) or `correct_expense` (reverses, then adds the right
  one). The original, the reason, the time and the user always stay on record.
* Every expense row also writes a row in `payments` (document_type 'expense'), so the
  money paid out appears in payment-mode and cash reports. A reversal writes a
  negative payment linked to the original.
* Each action is ONE transaction: the expense, its payment and the audit entry are
  saved together or not at all.

Needs the "manage_expenses" permission to change anything. Reading needs
"manage_expenses" or "view_reports".
"""

import os
from datetime import date, datetime

from core.audit import log_event
from core.permissions import PermissionDenied, has_permission, require_permission
from db.connection import transaction

DEFAULT_CATEGORIES = (
    "Rent", "Electricity", "Internet", "Salaries", "Transport", "Packaging",
    "Shopping bags", "Repairs", "Maintenance", "Bank charges", "Other",
)
EXPENSE_MODES = ("cash", "upi", "card", "bank_transfer", "cheque", "other")
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # SRS PAY-04: receipt attachment up to 5 MB


# ---------- helpers ----------

def _iso_date(value) -> str:
    text = value.isoformat() if isinstance(value, date) else str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("Expense date must be a valid date (YYYY-MM-DD).") from None


def _clean(value, label, *, required=True):
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{label} is required.")
    return text or None


def _check_attachment(path):
    if path is None:
        return None
    path = str(path)
    if not os.path.isfile(path):
        raise ValueError("The receipt file was not found.")
    if os.path.getsize(path) > MAX_ATTACHMENT_BYTES:
        raise ValueError("The receipt file is larger than 5 MB.")
    return path


def _validate(expense_date, category, description, amount_paise, payment_mode, attachment_path):
    date_text = _iso_date(expense_date)
    category = _clean(category, "Category")
    description = _clean(description, "Description")
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool) or amount_paise <= 0:
        raise ValueError("Amount must be greater than zero.")
    if payment_mode not in EXPENSE_MODES:
        raise ValueError(f"Payment method must be one of {EXPENSE_MODES}.")
    return date_text, category, description, _check_attachment(attachment_path)


def _insert_expense(
    conn, *, expense_date, category, description, amount_paise, payment_mode,
    reference, notes, attachment_path, user_id,
    reversal_of=None, corrects_id=None, reason=None, original_payment_id=None, paid_at=None,
):
    """Insert the expense row and its payment row. Returns the expense id."""
    cur = conn.execute(
        "INSERT INTO expenses(expense_date, category, description, amount_paise, payment_mode, reference, "
        "notes, attachment_path, reversal_of, corrects_id, reason, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (expense_date, category, description, amount_paise, payment_mode, reference, notes,
         attachment_path, reversal_of, corrects_id, reason, user_id),
    )
    expense_id = cur.lastrowid
    paid_at = paid_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, reversal_of, "
        "reason, user_id, created_at) VALUES ('expense', ?, ?, ?, ?, ?, ?, ?, ?)",
        (expense_id, payment_mode, amount_paise, reference, original_payment_id, reason, user_id, paid_at),
    )
    return expense_id


def _get_expense(conn, expense_id):
    row = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if row is None:
        raise ValueError("Expense not found.")
    return row


def _require_reversible(conn, expense):
    if expense["reversal_of"] is not None:
        raise ValueError("A reversal entry cannot be reversed.")
    if conn.execute("SELECT 1 FROM expenses WHERE reversal_of = ?", (expense["id"],)).fetchone():
        raise ValueError("This expense has already been reversed.")


def _reverse_in_transaction(conn, expense, reason, user_id):
    original_payment = conn.execute(
        "SELECT id FROM payments WHERE document_type = 'expense' AND document_id = ? AND reversal_of IS NULL",
        (expense["id"],),
    ).fetchone()
    return _insert_expense(
        conn,
        expense_date=expense["expense_date"], category=expense["category"],
        description=expense["description"], amount_paise=-expense["amount_paise"],
        payment_mode=expense["payment_mode"], reference=expense["reference"], notes=expense["notes"],
        attachment_path=expense["attachment_path"], user_id=user_id,
        reversal_of=expense["id"], reason=reason,
        original_payment_id=original_payment["id"] if original_payment else None,
    )


# ---------- actions ----------

def add_expense(
    conn, expense_date, category, description, amount_paise, *,
    user_id, payment_mode="cash", reference=None, notes=None, attachment_path=None,
) -> int:
    """Record an expense. Returns the expense id."""
    require_permission(conn, user_id, "manage_expenses", "record expenses")
    date_text, category, description, attachment = _validate(
        expense_date, category, description, amount_paise, payment_mode, attachment_path
    )
    with transaction(conn):
        expense_id = _insert_expense(
            conn, expense_date=date_text, category=category, description=description,
            amount_paise=amount_paise, payment_mode=payment_mode, reference=reference,
            notes=notes, attachment_path=attachment, user_id=user_id,
            paid_at=f"{date_text} {datetime.now().strftime('%H:%M:%S')}",
        )
        log_event(
            conn, "expense_added", user_id=user_id, entity="expense", entity_id=expense_id,
            after={"date": date_text, "category": category, "amount_paise": amount_paise, "mode": payment_mode},
        )
        return expense_id


def reverse_expense(conn, expense_id, reason, *, user_id) -> int:
    """Cancel an expense entered by mistake. Adds a linked negative entry. Returns its id."""
    require_permission(conn, user_id, "manage_expenses", "reverse expenses")
    reason = _clean(reason, "A reason")
    with transaction(conn):
        expense = _get_expense(conn, expense_id)
        _require_reversible(conn, expense)
        reversal_id = _reverse_in_transaction(conn, expense, reason, user_id)
        log_event(
            conn, "expense_reversed", user_id=user_id, entity="expense", entity_id=expense_id,
            after={"reversal_id": reversal_id, "reason": reason, "amount_paise": expense["amount_paise"]},
        )
        return reversal_id


def correct_expense(
    conn, expense_id, reason, *, user_id,
    expense_date=None, category=None, description=None, amount_paise=None,
    payment_mode=None, reference=None, notes=None, attachment_path=None,
) -> int:
    """Fix an expense: reverse the wrong entry and add the right one. Returns the NEW expense id.

    Fields left out keep their old value.
    """
    require_permission(conn, user_id, "manage_expenses", "correct expenses")
    reason = _clean(reason, "A reason")
    with transaction(conn):
        old = _get_expense(conn, expense_id)
        _require_reversible(conn, old)
        date_text, category_new, description_new, attachment = _validate(
            expense_date if expense_date is not None else old["expense_date"],
            category if category is not None else old["category"],
            description if description is not None else old["description"],
            amount_paise if amount_paise is not None else old["amount_paise"],
            payment_mode if payment_mode is not None else old["payment_mode"],
            attachment_path if attachment_path is not None else old["attachment_path"],
        )
        reversal_id = _reverse_in_transaction(conn, old, reason, user_id)
        new_id = _insert_expense(
            conn, expense_date=date_text, category=category_new, description=description_new,
            amount_paise=amount_paise if amount_paise is not None else old["amount_paise"],
            payment_mode=payment_mode if payment_mode is not None else old["payment_mode"],
            reference=reference if reference is not None else old["reference"],
            notes=notes if notes is not None else old["notes"],
            attachment_path=attachment, user_id=user_id, corrects_id=expense_id, reason=reason,
            paid_at=f"{date_text} {datetime.now().strftime('%H:%M:%S')}",
        )
        log_event(
            conn, "expense_corrected", user_id=user_id, entity="expense", entity_id=expense_id,
            before={"amount_paise": old["amount_paise"], "category": old["category"], "date": old["expense_date"]},
            after={"new_expense_id": new_id, "reversal_id": reversal_id, "reason": reason},
        )
        return new_id


# ---------- reading ----------

def _require_view(conn, user_id):
    if not (has_permission(conn, user_id, "manage_expenses") or has_permission(conn, user_id, "view_reports")):
        raise PermissionDenied("You are not allowed to view expenses.")


_STATUS_SQL = (
    "CASE WHEN e.reversal_of IS NOT NULL THEN 'reversal' "
    "WHEN EXISTS (SELECT 1 FROM expenses r WHERE r.reversal_of = e.id) THEN 'reversed' "
    "ELSE 'active' END"
)


def list_expenses(conn, *, user_id, date_from=None, date_to=None, category=None, search=None) -> list:
    """Expenses newest first, each with a `status`: 'active', 'reversed' or 'reversal'."""
    _require_view(conn, user_id)
    sql = f"SELECT e.*, {_STATUS_SQL} AS status FROM expenses e WHERE 1 = 1"
    args = []
    if date_from:
        sql += " AND e.expense_date >= ?"
        args.append(_iso_date(date_from))
    if date_to:
        sql += " AND e.expense_date <= ?"
        args.append(_iso_date(date_to))
    if category:
        sql += " AND lower(e.category) = lower(?)"
        args.append(category.strip())
    if search and search.strip():
        sql += " AND (e.description LIKE ? OR e.reference LIKE ? OR e.notes LIKE ?)"
        like = f"%{search.strip()}%"
        args += [like, like, like]
    sql += " ORDER BY e.expense_date DESC, e.id DESC"
    return conn.execute(sql, args).fetchall()


def expense_summary(conn, date_from, date_to, *, user_id, group_by="category") -> dict:
    """Net expense totals (reversals subtract). group_by: 'category', 'day' or 'month'.

    Returns {"rows": [{"key": ..., "total_paise": ...}], "total_paise": ...}.
    """
    _require_view(conn, user_id)
    keys = {"category": "e.category", "day": "e.expense_date", "month": "substr(e.expense_date, 1, 7)"}
    if group_by not in keys:
        raise ValueError(f"group_by must be one of {tuple(keys)}.")
    rows = conn.execute(
        f"SELECT {keys[group_by]} AS key, SUM(e.amount_paise) AS total_paise FROM expenses e "
        "WHERE e.expense_date >= ? AND e.expense_date <= ? GROUP BY key ORDER BY key",
        (_iso_date(date_from), _iso_date(date_to)),
    ).fetchall()
    result = [{"key": r["key"], "total_paise": r["total_paise"]} for r in rows]
    return {"rows": result, "total_paise": sum(r["total_paise"] for r in result)}
