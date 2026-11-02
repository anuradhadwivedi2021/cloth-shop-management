"""Suppliers: details, payments and the money we owe them (SRS 8, 14.2, PUR-07).

What we owe a supplier ("balance"; positive = we owe, negative = they owe us):

    opening balance
    + confirmed purchases
    - payments made (on bills and on account)
    - debit notes (goods returned)
    + refunds received from the supplier for returns

Everything is worked out from saved records, so it always matches the purchase and
return documents. Nothing is ever edited or deleted:

* A payment mistake is fixed with `reverse_supplier_payment` (a linked negative entry).
* A supplier is deactivated, never deleted; old purchases keep their supplier.
* Changing a supplier's contact details never changes old purchases.

Paying a supplier writes the supplier payment AND a row in `payments`
(document_type 'purchase'), in ONE transaction, so the cash register and payment-mode
reports see it. A payment against a bill can never be more than that bill still owes.

Permissions: paying, reversing and the opening balance need "manage_cash". Editing supplier
details needs "receive_purchase". Reading needs "manage_cash", "view_reports" or "receive_purchase".
"""

from datetime import date, datetime

from core.audit import log_event
from core.customers import validate_gstin
from core.numbering import financial_year, next_number
from core.permissions import PermissionDenied, has_permission, require_permission
from db.connection import transaction

VOUCHER_SERIES = "SPV"
SUPPLIER_MODES = ("cash", "upi", "card", "bank_transfer", "cheque", "other")
EDITABLE_FIELDS = (
    "name", "contact_person", "phone", "email", "address", "gstin", "state", "account_reference", "notes",
)
AGE_BUCKETS = ((30, "0-30"), (60, "31-60"), (90, "61-90"))  # anything older: "over 90"


# ---------- helpers ----------

def _iso_date(value) -> str:
    text = value.isoformat() if isinstance(value, date) else str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("Date must be a valid date (YYYY-MM-DD).") from None


def _text(value):
    value = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    return value or None


def _require_read(conn, user_id):
    if not any(has_permission(conn, user_id, p) for p in ("manage_cash", "view_reports", "receive_purchase")):
        raise PermissionDenied("You are not allowed to view supplier accounts.")


def _supplier(conn, supplier_id):
    row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if row is None:
        raise ValueError("Supplier not found.")
    return row


def _money(value, label="Amount"):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a whole number of paise greater than zero.")
    return value


def _bill_figures(conn, purchase) -> dict:
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE document_type = 'purchase' AND document_id = ?",
        (purchase["id"],),
    ).fetchone()[0]
    credit = conn.execute(
        "SELECT COALESCE(SUM(due_reduced_paise), 0) FROM purchase_returns WHERE purchase_id = ?",
        (purchase["id"],),
    ).fetchone()[0]
    return {
        "total_paise": purchase["total_paise"],
        "paid_paise": paid,
        "returns_credit_paise": credit,
        "outstanding_paise": max(purchase["total_paise"] - paid - credit, 0),
    }


# ---------- supplier details ----------

def update_supplier(conn, user_id, supplier_id, **changes) -> list:
    """Change supplier details. Returns the names of the fields that really changed."""
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Cannot change: {', '.join(sorted(unknown))}.")
    with transaction(conn):
        require_permission(conn, user_id, "receive_purchase", "edit suppliers")
        row = _supplier(conn, supplier_id)
        clean = {}
        for field, value in changes.items():
            value = _text(value)
            if field == "name" and not value:
                raise ValueError("Supplier name is required.")
            if field == "gstin" and value:
                value = validate_gstin(value)
            clean[field] = value
        diff = {f: v for f, v in clean.items() if row[f] != v}
        if not diff:
            return []
        sets = ", ".join(f"{f} = ?" for f in diff)
        conn.execute(
            f"UPDATE suppliers SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
            (*diff.values(), supplier_id),
        )
        log_event(
            conn, "supplier_updated", user_id=user_id, entity="supplier", entity_id=supplier_id,
            before={f: row[f] for f in diff}, after=diff,
        )
        return sorted(diff)


def set_supplier_active(conn, user_id, supplier_id, active: bool) -> None:
    """Deactivate (history stays) or reactivate a supplier."""
    with transaction(conn):
        require_permission(conn, user_id, "receive_purchase", "change a supplier's status")
        _supplier(conn, supplier_id)
        conn.execute(
            "UPDATE suppliers SET is_active = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (1 if active else 0, supplier_id),
        )
        log_event(
            conn, "supplier_activated" if active else "supplier_deactivated",
            user_id=user_id, entity="supplier", entity_id=supplier_id,
        )


def set_opening_balance(conn, user_id, supplier_id, amount_paise) -> None:
    """What we already owed the supplier before using this software (negative = they owed us).

    Only allowed while the supplier has no purchases, payments or debit notes, so the figure
    can never be changed to hide real activity.
    """
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
        raise ValueError("Opening balance must be a whole number of paise.")
    with transaction(conn):
        require_permission(conn, user_id, "manage_cash", "set a supplier's opening balance")
        row = _supplier(conn, supplier_id)
        busy = conn.execute(
            "SELECT (SELECT COUNT(*) FROM purchases WHERE supplier_id = ? AND status != 'draft') + "
            "(SELECT COUNT(*) FROM supplier_payments WHERE supplier_id = ?) + "
            "(SELECT COUNT(*) FROM purchase_returns WHERE supplier_id = ?)",
            (supplier_id, supplier_id, supplier_id),
        ).fetchone()[0]
        if busy:
            raise ValueError("The opening balance cannot be changed after the supplier has purchases or payments.")
        conn.execute(
            "UPDATE suppliers SET opening_balance_paise = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (amount_paise, supplier_id),
        )
        log_event(
            conn, "supplier_opening_balance_set", user_id=user_id, entity="supplier", entity_id=supplier_id,
            before={"opening_balance_paise": row["opening_balance_paise"]}, after={"opening_balance_paise": amount_paise},
        )


def search_suppliers(conn, *, user_id, query=None, include_inactive=False, limit=50) -> list:
    """Find suppliers by name, phone, GSTIN or account reference."""
    _require_read(conn, user_id)
    sql, args = "SELECT * FROM suppliers WHERE 1=1", []
    if not include_inactive:
        sql += " AND is_active = 1"
    text = (query or "").strip()
    if text:
        like = f"%{text}%"
        sql += " AND (name LIKE ? OR phone LIKE ? OR gstin LIKE ? OR account_reference LIKE ? OR contact_person LIKE ?)"
        args += [like] * 5
    return conn.execute(sql + " ORDER BY name LIMIT ?", (*args, limit)).fetchall()


# ---------- payments ----------

def pay_supplier(
    conn, user_id, supplier_id, amount_paise, mode, *,
    purchase_id=None, reference=None, notes=None, payment_date=None, idempotency_key=None,
) -> int:
    """Record a payment to a supplier. Returns the supplier payment id.

    purchase_id: pay a specific bill (cannot be more than it still owes). Without it the
    payment is "on account" (an advance), which reduces the supplier balance.
    """
    key = (idempotency_key or "").strip() or None
    amount_paise = _money(amount_paise)
    if mode not in SUPPLIER_MODES:
        raise ValueError(f"Choose a payment method: {', '.join(SUPPLIER_MODES)}.")
    when = _iso_date(payment_date or date.today())
    if when > date.today().isoformat():
        raise ValueError("A payment cannot be dated in the future.")

    with transaction(conn):
        if key:
            existing = conn.execute("SELECT id FROM supplier_payments WHERE idempotency_key = ?", (key,)).fetchone()
            if existing:
                return existing["id"]
        require_permission(conn, user_id, "manage_cash", "pay suppliers")
        _supplier(conn, supplier_id)
        if purchase_id is not None:
            purchase = conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
            if purchase is None or purchase["supplier_id"] != supplier_id:
                raise ValueError("That purchase does not belong to this supplier.")
            if purchase["status"] != "confirmed":
                raise ValueError("Only a confirmed purchase can be paid.")
            owed = _bill_figures(conn, purchase)["outstanding_paise"]
            if amount_paise > owed:
                raise ValueError(
                    f"This bill only has {owed / 100:,.2f} left to pay. Pay less, or record the rest as an advance."
                )
        fin_year = financial_year(date.fromisoformat(when))
        _, voucher = next_number(conn, VOUCHER_SERIES, fin_year, VOUCHER_SERIES)
        paid_at = f"{when} {datetime.now().strftime('%H:%M:%S')}"
        cur = conn.execute(
            "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, reason, user_id, created_at) "
            "VALUES ('purchase', ?, ?, ?, ?, ?, ?, ?)",
            (purchase_id, mode, amount_paise, _text(reference) or voucher, _text(notes), user_id, paid_at),
        )
        payment_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO supplier_payments(supplier_id, purchase_id, voucher_no, payment_date, mode, amount_paise, "
            "reference, notes, payment_id, idempotency_key, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (supplier_id, purchase_id, voucher, when, mode, amount_paise, _text(reference), _text(notes),
             payment_id, key, user_id),
        )
        log_event(
            conn, "supplier_payment", user_id=user_id, entity="supplier_payment", entity_id=cur.lastrowid,
            after={
                "voucher_no": voucher, "supplier_id": supplier_id, "purchase_id": purchase_id,
                "amount_paise": amount_paise, "mode": mode,
            },
        )
        return cur.lastrowid


def reverse_supplier_payment(conn, user_id, supplier_payment_id, reason) -> int:
    """Undo a payment by adding a linked opposite entry. The original stays. Returns the reversal id."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to reverse a payment.")
    with transaction(conn):
        require_permission(conn, user_id, "manage_cash", "reverse supplier payments")
        orig = conn.execute("SELECT * FROM supplier_payments WHERE id = ?", (supplier_payment_id,)).fetchone()
        if orig is None:
            raise ValueError("Supplier payment not found.")
        if orig["reversal_of"] is not None:
            raise ValueError("A reversal entry cannot be reversed.")
        if conn.execute("SELECT 1 FROM supplier_payments WHERE reversal_of = ?", (orig["id"],)).fetchone():
            raise ValueError("This payment has already been reversed.")
        paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, reversal_of, reason, "
            "user_id, created_at) VALUES ('purchase', ?, ?, ?, ?, ?, ?, ?, ?)",
            (orig["purchase_id"], orig["mode"], -orig["amount_paise"], orig["voucher_no"],
             orig["payment_id"], reason, user_id, paid_at),
        )
        cur = conn.execute(
            "INSERT INTO supplier_payments(supplier_id, purchase_id, payment_date, mode, amount_paise, reference, "
            "payment_id, reversal_of, reason, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (orig["supplier_id"], orig["purchase_id"], date.today().isoformat(), orig["mode"],
             -orig["amount_paise"], orig["voucher_no"], cur.lastrowid, orig["id"], reason, user_id),
        )
        log_event(
            conn, "supplier_payment_reversed", user_id=user_id, entity="supplier_payment", entity_id=cur.lastrowid,
            after={"reverses": orig["id"], "voucher_no": orig["voucher_no"], "reason": reason},
        )
        return cur.lastrowid


def list_supplier_payments(conn, supplier_id, *, user_id, date_from=None, date_to=None) -> list:
    _require_read(conn, user_id)
    sql, args = "SELECT * FROM supplier_payments WHERE supplier_id = ?", [supplier_id]
    if date_from:
        sql += " AND payment_date >= ?"
        args.append(_iso_date(date_from))
    if date_to:
        sql += " AND payment_date <= ?"
        args.append(_iso_date(date_to))
    return conn.execute(sql + " ORDER BY payment_date, id", args).fetchall()


# ---------- balances ----------

def _balance_parts(conn, supplier_id) -> dict:
    one = lambda sql: conn.execute(sql, (supplier_id,)).fetchone()[0]  # noqa: E731
    opening = one("SELECT opening_balance_paise FROM suppliers WHERE id = ?")
    purchases = one("SELECT COALESCE(SUM(total_paise), 0) FROM purchases WHERE supplier_id = ? AND status = 'confirmed'")
    bill_payments = one(
        "SELECT COALESCE(SUM(p.amount_paise), 0) FROM payments p JOIN purchases pu "
        "ON p.document_type = 'purchase' AND p.document_id = pu.id WHERE pu.supplier_id = ? AND pu.status = 'confirmed'"
    )
    on_account = one(
        "SELECT COALESCE(SUM(p.amount_paise), 0) FROM payments p JOIN supplier_payments sp ON sp.payment_id = p.id "
        "WHERE sp.supplier_id = ? AND sp.purchase_id IS NULL"
    )
    debit_notes = one("SELECT COALESCE(SUM(total_paise), 0) FROM purchase_returns WHERE supplier_id = ?")
    refunds = one(
        "SELECT COALESCE(-SUM(p.amount_paise), 0) FROM payments p JOIN purchase_returns r "
        "ON p.document_type = 'purchase_return' AND p.document_id = r.id WHERE r.supplier_id = ?"
    )
    return {
        "opening_paise": opening,
        "purchases_paise": purchases,
        "paid_on_bills_paise": bill_payments,
        "paid_on_account_paise": on_account,
        "debit_notes_paise": debit_notes,
        "refunds_received_paise": refunds,
        "balance_paise": opening + purchases - bill_payments - on_account - debit_notes + refunds,
    }


def supplier_balance(conn, supplier_id, *, user_id) -> dict:
    """The figures behind what we owe one supplier. `balance_paise` > 0 means we owe them."""
    _require_read(conn, user_id)
    _supplier(conn, supplier_id)
    return {"supplier_id": supplier_id, **_balance_parts(conn, supplier_id)}


def all_supplier_balances(conn, *, user_id, include_zero=False) -> list:
    """Every supplier with a balance, largest amount owed first (for the dues report and dashboard)."""
    _require_read(conn, user_id)
    out = []
    for s in conn.execute("SELECT id, name FROM suppliers ORDER BY name"):
        parts = _balance_parts(conn, s["id"])
        if include_zero or parts["balance_paise"] != 0:
            out.append({"supplier_id": s["id"], "name": s["name"], **parts})
    return sorted(out, key=lambda r: -r["balance_paise"])


def supplier_outstanding_bills(conn, supplier_id, *, user_id, today=None) -> list:
    """Confirmed bills that still have something to pay, oldest first, with their age bucket."""
    _require_read(conn, user_id)
    _supplier(conn, supplier_id)
    today = date.fromisoformat(_iso_date(today or date.today()))
    out = []
    for p in conn.execute(
        "SELECT * FROM purchases WHERE supplier_id = ? AND status = 'confirmed' ORDER BY invoice_date, id",
        (supplier_id,),
    ):
        fig = _bill_figures(conn, p)
        if fig["outstanding_paise"] <= 0:
            continue
        age = max((today - date.fromisoformat(p["invoice_date"])).days, 0)
        bucket = next((label for limit, label in AGE_BUCKETS if age <= limit), "over 90")
        out.append({
            "purchase_id": p["id"], "invoice_no": p["invoice_no"], "voucher_no": p["voucher_no"],
            "invoice_date": p["invoice_date"], "age_days": age, "age_bucket": bucket, **fig,
        })
    return out


def supplier_ledger(conn, supplier_id, *, user_id, date_from=None, date_to=None) -> dict:
    """The supplier's account, line by line, with a running balance.

    Each entry: date, kind, reference, debit_paise (we owe more), credit_paise (we owe less), balance_paise.
    With a date range the first line is the balance brought forward.
    """
    _require_read(conn, user_id)
    sup = _supplier(conn, supplier_id)
    start = _iso_date(date_from) if date_from else None
    end = _iso_date(date_to) if date_to else None

    rows = []  # (date, order, id, kind, reference, debit, credit)
    for p in conn.execute(
        "SELECT id, invoice_date, invoice_no, voucher_no, total_paise FROM purchases "
        "WHERE supplier_id = ? AND status = 'confirmed'", (supplier_id,)
    ):
        rows.append((p["invoice_date"], 1, p["id"], "purchase", f"{p['voucher_no']} / {p['invoice_no']}", p["total_paise"], 0))
    for p in conn.execute(
        "SELECT p.id, substr(p.created_at, 1, 10) AS d, p.amount_paise, p.reference, p.reversal_of FROM payments p "
        "JOIN purchases pu ON p.document_type = 'purchase' AND p.document_id = pu.id "
        "WHERE pu.supplier_id = ? AND pu.status = 'confirmed'", (supplier_id,)
    ):
        rows.append(_payment_row(p, "payment on bill"))
    for p in conn.execute(
        "SELECT p.id, substr(p.created_at, 1, 10) AS d, p.amount_paise, p.reference, p.reversal_of FROM payments p "
        "JOIN supplier_payments sp ON sp.payment_id = p.id WHERE sp.supplier_id = ? AND sp.purchase_id IS NULL",
        (supplier_id,),
    ):
        rows.append(_payment_row(p, "advance payment"))
    for r in conn.execute(
        "SELECT id, substr(created_at, 1, 10) AS d, debit_note_no, total_paise FROM purchase_returns WHERE supplier_id = ?",
        (supplier_id,),
    ):
        rows.append((r["d"], 3, r["id"], "debit note", r["debit_note_no"], 0, r["total_paise"]))
    for p in conn.execute(
        "SELECT p.id, substr(p.created_at, 1, 10) AS d, p.amount_paise, r.debit_note_no FROM payments p "
        "JOIN purchase_returns r ON p.document_type = 'purchase_return' AND p.document_id = r.id "
        "WHERE r.supplier_id = ?", (supplier_id,)
    ):
        rows.append((p["d"], 4, p["id"], "refund received", p["debit_note_no"], -p["amount_paise"], 0))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))

    balance = sup["opening_balance_paise"]
    brought = balance  # balance brought forward to the first line shown
    entries = []
    for day, _, _, kind, ref, debit, credit in rows:
        if start and day < start:
            balance += debit - credit
            brought = balance
            continue
        if end and day > end:
            break
        balance += debit - credit
        entries.append({
            "date": day, "kind": kind, "reference": ref,
            "debit_paise": debit, "credit_paise": credit, "balance_paise": balance,
        })
    return {
        "supplier_id": supplier_id, "name": sup["name"],
        "opening_paise": brought, "entries": entries, "closing_paise": balance,
    }


def _payment_row(p, kind):
    amount = p["amount_paise"]
    label = kind if amount > 0 else f"{kind} reversed"
    return (p["d"], 2, p["id"], label, p["reference"], -amount if amount < 0 else 0, amount if amount > 0 else 0)
