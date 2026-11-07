"""Credit sales: what customers owe, receiving their payments, credit limits, due dates and ageing.

Single source of truth: a bill's due is NEVER stored. It is always
    bill total - payments recorded on the bill - dues cleared by returns (credit notes)
(the same rule core.returns uses). Receiving money therefore writes ordinary `payments` rows on the
bill, so the bill, the cash/payment reports and the customer history all agree.

What this module adds on top:

* numbered receipts (series RCT) with the bill(s) each one paid; a receipt is never edited or deleted,
  a mistake is cancelled by a reversal receipt (negative, linked to the original, with a reason);
* a credit limit per customer, enforced when a bill is saved (core.guarded.sell calls
  `apply_credit_terms_to_sale`); above the limit the owner's PIN is needed and the override is logged;
* a due date per credit bill (append-only history, latest row wins);
* ageing of what is owed: 0-30, 31-60 and over 60 days, counted from the bill date.

Every write here is ONE transaction that also writes the audit log. Permissions are checked here:
receive_customer_payment for receipts, reversals and due dates; manage_settings for credit terms.
The audit log never receives names or phone numbers, only ids and amounts.

All money is integer paise.
"""

from datetime import date, datetime, timedelta

from core.audit import log_event
from core.money import format_rupees
from core.numbering import financial_year, next_number
from core.permissions import PermissionDenied, has_permission, require_permission
from core.sales import enabled_payment_modes
from db.connection import transaction

RECEIPT_SERIES = "RCT"
RECEIPT_MODES = ("cash", "upi", "card", "bank_transfer", "cheque", "other")
# Who may look at dues (any one is enough). Receiving money needs receive_customer_payment.
READ_PERMISSIONS = ("view_customers", "view_reports", "receive_customer_payment")
MAX_CREDIT_DAYS = 365


class CreditLimitExceeded(PermissionDenied):
    """The bill would take the customer over the credit limit (or credit is not allowed for them).

    The owner's PIN can approve it. `reason` is 'limit_exceeded' or 'credit_not_enabled'.
    """

    def __init__(self, message, *, reason, outstanding_paise, bill_due_paise, limit_paise):
        super().__init__(message)
        self.reason = reason
        self.outstanding_paise = outstanding_paise
        self.bill_due_paise = bill_due_paise
        self.limit_paise = limit_paise


# ---------------------------------------------------------------- small helpers

def _date(value, what="Date") -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{what} must be a valid date (YYYY-MM-DD).") from None


def _paise(value, what="Amount") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{what} must be more than zero.")
    return value


def _require_read(conn, user_id):
    if not any(has_permission(conn, user_id, p) for p in READ_PERMISSIONS):
        raise PermissionDenied("You are not allowed to view customer dues.")


def _customer(conn, customer_id):
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise ValueError("Customer not found.")
    return row


# ---------------------------------------------------------------- dues (computed, never stored)

_OPEN_BILLS_SQL = """
SELECT s.id AS sale_id, s.invoice_no, s.customer_id, s.created_at, s.total_paise,
       COALESCE((SELECT SUM(p.amount_paise) FROM payments p
                 WHERE p.document_type = 'sale' AND p.document_id = s.id), 0) AS paid_paise,
       COALESCE((SELECT SUM(r.due_adjusted_paise) FROM sale_returns r WHERE r.sale_id = s.id), 0) AS returned_paise,
       d.due_date AS set_due_date,
       c.name, c.phone, c.credit_enabled, c.credit_limit_paise, c.default_credit_days
FROM sales s
JOIN customers c ON c.id = s.customer_id
LEFT JOIN sale_due_dates d ON d.id = (SELECT MAX(x.id) FROM sale_due_dates x WHERE x.sale_id = s.id)
WHERE s.status = 'completed' {where}
ORDER BY s.created_at, s.id
"""


def _open_bills(conn, *, customer_id=None, sale_id=None, as_of=None) -> list:
    """Bills with something still owed, oldest first, with due date, age and ageing bucket."""
    today = _date(as_of, "As-of date")
    where, params = "", []
    if customer_id is not None:
        where += " AND s.customer_id = ?"
        params.append(customer_id)
    if sale_id is not None:
        where += " AND s.id = ?"
        params.append(sale_id)
    bills = []
    for row in conn.execute(_OPEN_BILLS_SQL.format(where=where), params):
        due = max(row["total_paise"] - row["paid_paise"] - row["returned_paise"], 0)
        if due == 0:
            continue
        bill_date = date.fromisoformat(row["created_at"][:10])
        due_date = (
            date.fromisoformat(row["set_due_date"]) if row["set_due_date"]
            else bill_date + timedelta(days=row["default_credit_days"])
        )
        age = max((today - bill_date).days, 0)
        bills.append({
            "sale_id": row["sale_id"], "invoice_no": row["invoice_no"], "customer_id": row["customer_id"],
            "name": row["name"], "phone": row["phone"],
            "bill_date": bill_date.isoformat(), "due_date": due_date.isoformat(),
            "total_paise": row["total_paise"], "paid_paise": row["paid_paise"],
            "returned_paise": row["returned_paise"], "due_paise": due,
            "age_days": age, "bucket": _bucket(age),
            "overdue_days": max((today - due_date).days, 0),
            "credit_enabled": bool(row["credit_enabled"]), "credit_limit_paise": row["credit_limit_paise"],
        })
    return bills


def _bucket(age_days: int) -> str:
    if age_days <= 30:
        return "0-30"
    if age_days <= 60:
        return "31-60"
    return "over 60"


def _ageing(bills) -> dict:
    result = {"bucket_0_30_paise": 0, "bucket_31_60_paise": 0, "bucket_over_60_paise": 0, "total_paise": 0}
    keys = {"0-30": "bucket_0_30_paise", "31-60": "bucket_31_60_paise", "over 60": "bucket_over_60_paise"}
    for bill in bills:
        result[keys[bill["bucket"]]] += bill["due_paise"]
        result["total_paise"] += bill["due_paise"]
    return result


def customer_outstanding_paise(conn, customer_id, *, exclude_sale_id=None) -> int:
    """What the customer owes right now over all their bills (never below zero per bill)."""
    return sum(
        b["due_paise"] for b in _open_bills(conn, customer_id=customer_id) if b["sale_id"] != exclude_sale_id
    )


def customer_dues(conn, customer_id, *, user_id, as_of=None) -> dict:
    """One customer's dues: limit, what is owed, each open bill (oldest first) and the ageing."""
    _require_read(conn, user_id)
    row = _customer(conn, customer_id)
    bills = _open_bills(conn, customer_id=customer_id, as_of=as_of)
    ageing = _ageing(bills)
    limit = row["credit_limit_paise"]
    return {
        "customer_id": customer_id,
        "credit_enabled": bool(row["credit_enabled"]),
        "credit_limit_paise": limit,
        "default_credit_days": row["default_credit_days"],
        "outstanding_paise": ageing["total_paise"],
        "available_credit_paise": max(limit - ageing["total_paise"], 0) if row["credit_enabled"] else 0,
        "overdue_paise": sum(b["due_paise"] for b in bills if b["overdue_days"] > 0),
        "bills": bills,
        "ageing": ageing,
    }


def dues_report(conn, *, user_id, as_of=None, overdue_only=False) -> dict:
    """Everyone who owes money: one row per customer (biggest dues first) plus shop totals.

    Rows hold names and phone numbers (for calling customers), so each customer listed is written to
    the customer access log, and an export of this report must be treated as personal data.
    """
    _require_read(conn, user_id)
    bills = _open_bills(conn, as_of=as_of)
    if overdue_only:
        bills = [b for b in bills if b["overdue_days"] > 0]
    by_customer = {}
    for bill in bills:
        by_customer.setdefault(bill["customer_id"], []).append(bill)

    rows = []
    for customer_id, items in by_customer.items():
        ageing = _ageing(items)
        first = items[0]
        rows.append({
            "customer_id": customer_id, "name": first["name"], "phone": first["phone"],
            "credit_limit_paise": first["credit_limit_paise"], "open_bills": len(items),
            "oldest_bill_date": min(b["bill_date"] for b in items),
            "outstanding_paise": ageing["total_paise"],
            "overdue_paise": sum(b["due_paise"] for b in items if b["overdue_days"] > 0),
            **ageing,
        })
    rows.sort(key=lambda r: (-r["outstanding_paise"], r["customer_id"]))

    if rows:
        with transaction(conn):
            for row in rows:
                conn.execute(
                    "INSERT INTO customer_access_log(customer_id, user_id, action) VALUES (?,?,?)",
                    (row["customer_id"], user_id, "dues_report"),
                )
    totals = _ageing(bills)
    totals["customers"] = len(rows)
    totals["overdue_paise"] = sum(r["overdue_paise"] for r in rows)
    return {"as_of": _date(as_of, "As-of date").isoformat(), "rows": rows, "totals": totals}


# ---------------------------------------------------------------- credit terms and the limit

def set_credit_terms(conn, customer_id, *, user_id, credit_enabled, credit_limit_paise, default_credit_days=30):
    """Owner sets whether a customer may buy on credit, how much, and the usual pay-by days."""
    if not isinstance(credit_limit_paise, int) or isinstance(credit_limit_paise, bool) or credit_limit_paise < 0:
        raise ValueError("Credit limit must be a whole amount in paise, zero or more.")
    if (not isinstance(default_credit_days, int) or isinstance(default_credit_days, bool)
            or not 0 <= default_credit_days <= MAX_CREDIT_DAYS):
        raise ValueError(f"Credit days must be between 0 and {MAX_CREDIT_DAYS}.")
    with transaction(conn):
        require_permission(conn, user_id, "manage_settings", "change credit terms")
        row = _customer(conn, customer_id)
        before = {
            "credit_enabled": bool(row["credit_enabled"]),
            "credit_limit_paise": row["credit_limit_paise"],
            "default_credit_days": row["default_credit_days"],
        }
        after = {
            "credit_enabled": bool(credit_enabled),
            "credit_limit_paise": credit_limit_paise,
            "default_credit_days": default_credit_days,
        }
        if before == after:
            return
        conn.execute(
            "UPDATE customers SET credit_enabled = ?, credit_limit_paise = ?, default_credit_days = ? WHERE id = ?",
            (int(bool(credit_enabled)), credit_limit_paise, default_credit_days, customer_id),
        )
        log_event(conn, "credit_terms_changed", user_id=user_id, entity="customer", entity_id=customer_id,
                  before=before, after=after)


def apply_credit_terms_to_sale(conn, sale_id, *, user_id, override_user_id=None, due_date=None) -> None:
    """Check the credit limit for a just-saved bill and record its due date.

    Must be called inside the same transaction that saved the bill (core.guarded.sell does this), so a
    refused bill is rolled back completely. Does nothing for a bill that was paid in full.
    Raises CreditLimitExceeded unless `override_user_id` (the approving owner) is given; an override
    is written to the audit log.
    """
    if not conn.in_transaction:
        raise RuntimeError("apply_credit_terms_to_sale must be called inside a transaction.")
    sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if sale is None:
        raise ValueError("Bill not found.")
    bills = _open_bills(conn, sale_id=sale_id)
    if not bills:
        return  # fully paid: nothing owed, no credit used
    bill = bills[0]
    customer = _customer(conn, bill["customer_id"])
    before = customer_outstanding_paise(conn, bill["customer_id"], exclude_sale_id=sale_id)
    limit = customer["credit_limit_paise"]
    violation = None
    if not customer["credit_enabled"]:
        violation = "credit_not_enabled"
    elif before + bill["due_paise"] > limit:
        violation = "limit_exceeded"

    if violation:
        if override_user_id is None:
            if violation == "credit_not_enabled":
                text = "Credit is not allowed for this customer. The owner's PIN is needed."
            else:
                text = (
                    f"Credit limit exceeded: this bill adds {format_rupees(bill['due_paise'])} to "
                    f"{format_rupees(before)} already due; the limit is {format_rupees(limit)}. "
                    "The owner's PIN is needed."
                )
            raise CreditLimitExceeded(
                text, reason=violation, outstanding_paise=before, bill_due_paise=bill["due_paise"], limit_paise=limit,
            )
        log_event(
            conn, "credit_limit_override", user_id=user_id, entity="sale", entity_id=sale_id,
            after={"approved_by": override_user_id, "reason": violation, "outstanding_before_paise": before,
                   "bill_due_paise": bill["due_paise"], "limit_paise": limit},
        )

    bill_date = date.fromisoformat(sale["created_at"][:10])
    if due_date is None:
        chosen = bill_date + timedelta(days=customer["default_credit_days"])
    else:
        chosen = _date(due_date, "Due date")
        if chosen < bill_date:
            raise ValueError("The due date cannot be before the bill date.")
    conn.execute(
        "INSERT INTO sale_due_dates(sale_id, due_date, reason, user_id) VALUES (?,?,?,?)",
        (sale_id, chosen.isoformat(), "bill created", user_id),
    )


def set_due_date(conn, sale_id, due_date, *, user_id, reason) -> None:
    """Move the pay-by date of a bill that is still unpaid. The old date stays in the history."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Give a reason for changing the due date.")
    chosen = _date(due_date, "Due date")
    with transaction(conn):
        require_permission(conn, user_id, "receive_customer_payment", "change a due date")
        bills = _open_bills(conn, sale_id=sale_id)
        if not bills:
            raise ValueError("This bill has nothing due.")
        if chosen < date.fromisoformat(bills[0]["bill_date"]):
            raise ValueError("The due date cannot be before the bill date.")
        old = bills[0]["due_date"]
        if old == chosen.isoformat():
            return
        conn.execute(
            "INSERT INTO sale_due_dates(sale_id, due_date, reason, user_id) VALUES (?,?,?,?)",
            (sale_id, chosen.isoformat(), reason, user_id),
        )
        log_event(conn, "due_date_changed", user_id=user_id, entity="sale", entity_id=sale_id,
                  before={"due_date": old}, after={"due_date": chosen.isoformat(), "reason": reason})


# ---------------------------------------------------------------- receiving money

def receive_payment(
    conn, *, amount_paise, mode, user_id, customer_id=None, sale_id=None, reference=None, idempotency_key=None,
) -> int:
    """Take a payment from a customer. Returns the receipt id.

    With `sale_id` the money pays that bill; without it, the customer's oldest bills are paid first.
    The amount can never be more than what is owed (no money is left without a bill). One transaction:
    receipt number, receipt, allocations, the payments rows on the bill(s) and the audit entry are
    saved together or not at all. The same `idempotency_key` returns the receipt already saved.
    """
    amount = _paise(amount_paise)
    accepted = tuple(m for m in RECEIPT_MODES if m in enabled_payment_modes(conn))
    if mode not in accepted:
        raise ValueError(f"Payment mode '{mode}' is not available. Use one of: {', '.join(accepted)}.")
    if customer_id is None and sale_id is None:
        raise ValueError("Select a customer or a bill.")
    key = (idempotency_key or "").strip() or None

    with transaction(conn):
        require_permission(conn, user_id, "receive_customer_payment", "receive customer payments")
        if key:
            existing = conn.execute(
                "SELECT id FROM customer_receipts WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                return existing["id"]

        if sale_id is not None:
            owner_row = conn.execute("SELECT customer_id FROM sales WHERE id = ?", (sale_id,)).fetchone()
            if owner_row is None:
                raise ValueError("Bill not found.")
            if customer_id is not None and owner_row["customer_id"] != customer_id:
                raise ValueError("This bill does not belong to the selected customer.")
            customer_id = owner_row["customer_id"]
            if customer_id is None:
                raise ValueError("This bill has no customer, so nothing can be due on it.")
        _customer(conn, customer_id)

        bills = _open_bills(conn, customer_id=customer_id, sale_id=sale_id)
        owed = sum(b["due_paise"] for b in bills)
        if owed == 0:
            raise ValueError("Nothing is due on this " + ("bill." if sale_id is not None else "customer."))
        if amount > owed:
            raise ValueError(f"The customer owes only {format_rupees(owed)}. Enter that amount or less.")

        allocations, left = [], amount
        for bill in bills:  # oldest first
            if left == 0:
                break
            take = min(left, bill["due_paise"])
            allocations.append((bill, take))
            left -= take

        fin_year = financial_year(date.today())
        number, receipt_no = next_number(conn, RECEIPT_SERIES, fin_year, RECEIPT_SERIES)
        receipt_id = conn.execute(
            "INSERT INTO customer_receipts(series, number, receipt_no, fin_year, customer_id, mode, amount_paise, "
            "reference, user_id, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (RECEIPT_SERIES, number, receipt_no, fin_year, customer_id, mode, amount, reference, user_id, key),
        ).lastrowid
        for bill, take in allocations:
            payment_id = conn.execute(
                "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, user_id) "
                "VALUES ('sale', ?, ?, ?, ?, ?)",
                (bill["sale_id"], mode, take, receipt_no, user_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO customer_receipt_allocations(receipt_id, sale_id, payment_id, amount_paise) "
                "VALUES (?,?,?,?)",
                (receipt_id, bill["sale_id"], payment_id, take),
            )
        log_event(
            conn, "customer_payment_received", user_id=user_id, entity="customer_receipt", entity_id=receipt_id,
            after={"receipt_no": receipt_no, "customer_id": customer_id, "amount_paise": amount, "mode": mode,
                   "bills": [b["sale_id"] for b, _ in allocations]},
        )
        return receipt_id


def reverse_receipt(conn, receipt_id, *, user_id, reason) -> int:
    """Cancel a receipt (wrong amount, bounced cheque, ...). Returns the reversal receipt id.

    The original stays as it was. A reversal receipt with its own RCT number, the opposite amount and
    opposite payments rows is added, so the bills owe the money again. A receipt can be reversed once.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Give a reason for reversing this receipt.")
    with transaction(conn):
        require_permission(conn, user_id, "receive_customer_payment", "reverse customer receipts")
        original = conn.execute("SELECT * FROM customer_receipts WHERE id = ?", (receipt_id,)).fetchone()
        if original is None:
            raise ValueError("Receipt not found.")
        if original["reversal_of"] is not None:
            raise ValueError("A reversal cannot be reversed. Take a new payment instead.")
        if conn.execute("SELECT 1 FROM customer_receipts WHERE reversal_of = ?", (receipt_id,)).fetchone():
            raise ValueError("This receipt is already reversed.")

        fin_year = financial_year(date.today())
        number, receipt_no = next_number(conn, RECEIPT_SERIES, fin_year, RECEIPT_SERIES)
        reversal_id = conn.execute(
            "INSERT INTO customer_receipts(series, number, receipt_no, fin_year, customer_id, mode, amount_paise, "
            "reference, reversal_of, reason, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (RECEIPT_SERIES, number, receipt_no, fin_year, original["customer_id"], original["mode"],
             -original["amount_paise"], original["receipt_no"], receipt_id, reason, user_id),
        ).lastrowid
        for alloc in conn.execute(
            "SELECT * FROM customer_receipt_allocations WHERE receipt_id = ? ORDER BY id", (receipt_id,)
        ).fetchall():
            payment_id = conn.execute(
                "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, reversal_of, "
                "reason, user_id) VALUES ('sale', ?, ?, ?, ?, ?, ?, ?)",
                (alloc["sale_id"], original["mode"], -alloc["amount_paise"], receipt_no, alloc["payment_id"],
                 reason, user_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO customer_receipt_allocations(receipt_id, sale_id, payment_id, amount_paise) "
                "VALUES (?,?,?,?)",
                (reversal_id, alloc["sale_id"], payment_id, -alloc["amount_paise"]),
            )
        log_event(
            conn, "customer_receipt_reversed", user_id=user_id, entity="customer_receipt", entity_id=reversal_id,
            before={"receipt_no": original["receipt_no"], "amount_paise": original["amount_paise"]},
            after={"reversal_no": receipt_no, "reason": reason},
        )
        return reversal_id


# ---------------------------------------------------------------- reading receipts

def get_receipt(conn, receipt_id, *, user_id) -> dict:
    """A receipt with the bills it paid, ready to print. Includes the customer's name and phone."""
    _require_read(conn, user_id)
    row = conn.execute(
        "SELECT r.*, c.name AS customer_name, c.phone AS customer_phone "
        "FROM customer_receipts r JOIN customers c ON c.id = r.customer_id WHERE r.id = ?",
        (receipt_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Receipt not found.")
    receipt = dict(row)
    receipt["allocations"] = [
        dict(a) for a in conn.execute(
            "SELECT a.sale_id, s.invoice_no, a.amount_paise FROM customer_receipt_allocations a "
            "JOIN sales s ON s.id = a.sale_id WHERE a.receipt_id = ? ORDER BY a.id", (receipt_id,),
        )
    ]
    reversed_by = conn.execute(
        "SELECT id, receipt_no FROM customer_receipts WHERE reversal_of = ?", (receipt_id,)
    ).fetchone()
    receipt["is_reversal"] = receipt["reversal_of"] is not None
    receipt["is_reversed"] = reversed_by is not None
    receipt["reversed_by_receipt_no"] = reversed_by["receipt_no"] if reversed_by else None
    receipt["customer_outstanding_paise"] = customer_outstanding_paise(conn, receipt["customer_id"])
    return receipt


def list_receipts(conn, customer_id, *, user_id) -> list:
    """A customer's receipts and reversals, newest first."""
    _require_read(conn, user_id)
    _customer(conn, customer_id)
    return [
        dict(r) for r in conn.execute(
            "SELECT r.id, r.receipt_no, r.mode, r.amount_paise, r.reversal_of, r.reason, r.created_at, "
            "EXISTS(SELECT 1 FROM customer_receipts x WHERE x.reversal_of = r.id) AS is_reversed "
            "FROM customer_receipts r WHERE r.customer_id = ? ORDER BY r.id DESC", (customer_id,),
        )
    ]
