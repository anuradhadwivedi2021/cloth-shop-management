"""Customers: profiles, consent, history, and the customer's data rights (DPDP).

Rules this module enforces:

* A walk-in sale needs no profile. A profile needs a phone number and the
  customer's consent (the purpose is stored with the date).
* Customers are never deleted. They are archived (is_active = 0) or, when the
  customer asks, anonymised, so old bills and credit notes keep a valid reference.
* The audit log is permanent and hash-chained, so it never receives a customer's
  name, phone or other personal details: only ids and the names of changed fields.
  Who looked at a customer's data goes to customer_access_log instead.
* Every function checks the user's permission here, not only in the screens:
  view_customers / edit_customers for everyday work, manage_customer_data
  (owner by default) for export, erase and the retention review.

Legal wording for the privacy notice and the exact retention rules must be
reviewed by the shop's lawyer and accountant. These are software hooks, not legal advice.
"""

import calendar
import json
import os
import re
import string
from datetime import date, datetime

from core.audit import log_event
from core.permissions import require_permission
from core.returns import _due_remaining, _sale_paid, get_store_credit_balance
from core.settings import get_bool, get_setting
from db.connection import transaction

DEFAULT_CONSENT_PURPOSE = "Billing, returns and store credit for purchases at this shop."
DEFAULT_RETENTION_MONTHS = 24
# Only these reasons are accepted for an erasure, so no free text (and no personal
# detail) ever reaches the permanent audit log.
ERASE_REASONS = ("customer_request", "consent_withdrawn", "retention_review", "other")

_EDITABLE = ("name", "phone", "address", "gstin", "birthday", "preferred_size", "notes")
_GSTIN_SHAPE = re.compile(r"^(\d{2})[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_GSTIN_CHARS = string.digits + string.ascii_uppercase
_STATE_CODES = {f"{n:02d}" for n in range(1, 39)} | {"97", "99"}


class DuplicateCustomerError(ValueError):
    """A profile with this phone number already exists (the user can review it)."""

    def __init__(self, message, existing_id):
        super().__init__(message)
        self.existing_id = existing_id


# ---------------------------------------------------------------- cleaning

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rs(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def _text(value):
    """Trimmed text, or None when empty."""
    return str(value).strip() or None if value is not None else None


def _phone_digits(value) -> str:
    """Digits only, without a +91 or leading 0 in front of a full number."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


def normalise_phone(value) -> str:
    """Keep the 10 digits of an Indian number; +91 and a leading 0 are accepted and dropped."""
    digits = _phone_digits(value)
    if len(digits) != 10:
        raise ValueError("Enter a 10-digit phone number.")
    return digits


def validate_gstin(value) -> str:
    """Return the GSTIN in capitals, or raise ValueError if the format or check character is wrong."""
    gstin = (value or "").strip().upper()
    match = _GSTIN_SHAPE.match(gstin)
    if match is None or match.group(1) not in _STATE_CODES:
        raise ValueError("GSTIN looks wrong. It has 15 characters, for example 27AAPFU0939F1ZV.")
    total = 0
    for position, char in enumerate(gstin[:14]):
        product = _GSTIN_CHARS.index(char) * (2 if position % 2 else 1)
        total += product // 36 + product % 36
    if _GSTIN_CHARS[(36 - total % 36) % 36] != gstin[14]:
        raise ValueError("GSTIN check character does not match. Please re-check the number.")
    return gstin


def _clean_birthday(value, today=None):
    """YYYY-MM-DD, or MM-DD when the customer does not want to give the year."""
    value = _text(value)
    if value is None:
        return None
    if re.fullmatch(r"\d{2}-\d{2}", value):
        try:
            date(2000, int(value[:2]), int(value[3:]))  # 2000 is a leap year, so 02-29 is allowed
        except ValueError:
            raise ValueError("Birthday is not a real date.") from None
        return value
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Birthday must look like 2001-03-25 (or 03-25 without the year).")
    try:
        born = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Birthday is not a real date.") from None
    if born > (today or date.today()) or born.year < 1900:
        raise ValueError("Birthday is not a believable date.")
    return value


def _clean_fields(changes: dict) -> dict:
    out = {}
    for key, value in changes.items():
        if key == "phone":
            out[key] = normalise_phone(value)
        elif key == "gstin":
            out[key] = validate_gstin(value) if _text(value) else None
        elif key == "birthday":
            out[key] = _clean_birthday(value)
        else:
            out[key] = _text(value)
    return out


# ---------------------------------------------------------------- helpers

def _row(conn, customer_id):
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise ValueError("Customer not found.")
    return row


def _editable(row):
    if row["anonymised_at"] is not None:
        raise ValueError("This customer's personal data was erased, so the profile cannot be changed.")


def _check_duplicate_phone(conn, phone, *, exclude_id=None):
    row = conn.execute(
        "SELECT id FROM customers WHERE phone = ? AND anonymised_at IS NULL AND id IS NOT ? LIMIT 1",
        (phone, exclude_id),
    ).fetchone()
    if row is not None:
        raise DuplicateCustomerError(
            "A customer with this phone number already exists. Open that profile instead, "
            "or confirm it is a different person (for example a shared family phone).",
            row["id"],
        )


def _log_access(conn, customer_id, user_id, action):
    conn.execute(
        "INSERT INTO customer_access_log(customer_id, user_id, action) VALUES (?,?,?)",
        (customer_id, user_id, action),
    )


def _bills_and_returns(conn, customer_id):
    """Bills, credit notes and the amount still owed (after returns), for history and export."""
    bills, outstanding = [], 0
    for sale in conn.execute("SELECT * FROM sales WHERE customer_id = ? ORDER BY id DESC", (customer_id,)):
        paid = _sale_paid(conn, sale["id"])
        due = _due_remaining(conn, sale) if sale["status"] == "completed" else 0
        outstanding += due
        bills.append({
            "sale_id": sale["id"], "invoice_no": sale["invoice_no"], "date": sale["created_at"],
            "status": sale["status"], "total_paise": sale["total_paise"], "paid_paise": paid,
            "due_paise": due,
        })
    returns = [
        dict(r) for r in conn.execute(
            "SELECT id, credit_note_no, sale_id, outcome, total_paise, store_credit_paise, created_at "
            "FROM sale_returns WHERE customer_id = ? ORDER BY id DESC", (customer_id,),
        )
    ]
    return bills, returns, outstanding


# ---------------------------------------------------------------- create and edit

def create_customer(
    conn, phone, *, user_id, name=None, address=None, gstin=None, birthday=None,
    preferred_size=None, notes=None, consent_given=False, consent_purpose=None,
    allow_duplicate_phone=False,
) -> int:
    """Save a customer profile. Returns the customer id.

    Only the phone number is required, plus the customer's consent. For a one-time
    sale do not create a profile: bill it as a walk-in.
    """
    require_permission(conn, user_id, "edit_customers", "create customers")
    if not consent_given:
        raise ValueError(
            "The customer's consent is needed to save a profile. "
            "For a one-time sale, bill it as a walk-in without a profile."
        )
    fields = _clean_fields({
        "phone": phone, "name": name, "address": address, "gstin": gstin,
        "birthday": birthday, "preferred_size": preferred_size, "notes": notes,
    })
    with transaction(conn):
        if not allow_duplicate_phone:
            _check_duplicate_phone(conn, fields["phone"])
        purpose = _text(consent_purpose) or get_setting(conn, "customer_consent_purpose") or DEFAULT_CONSENT_PURPOSE
        now = _now()
        cur = conn.execute(
            "INSERT INTO customers(name, phone, address, gstin, birthday, preferred_size, notes, "
            "consent_given, consent_date, consent_purpose, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?,?)",
            (fields["name"], fields["phone"], fields["address"], fields["gstin"], fields["birthday"],
             fields["preferred_size"], fields["notes"], now, purpose, now),
        )
        log_event(conn, "customer_created", user_id=user_id, entity="customer", entity_id=cur.lastrowid,
                  after={"consent": True})
        return cur.lastrowid


def update_customer(conn, customer_id, *, user_id, **changes) -> list:
    """Change profile fields (name, phone, address, gstin, birthday, preferred_size, notes).

    Pass None or "" to clear an optional field. Returns the names of the fields that
    really changed (an empty list means nothing was saved). The audit log records
    only those field names, never the values.
    """
    unknown = set(changes) - set(_EDITABLE)
    if unknown:
        raise ValueError("These cannot be changed here: " + ", ".join(sorted(unknown)) + ".")
    require_permission(conn, user_id, "edit_customers", "edit customers")
    cleaned = _clean_fields(changes)
    with transaction(conn):
        row = _row(conn, customer_id)
        _editable(row)
        changed = {key: value for key, value in cleaned.items() if row[key] != value}
        if not changed:
            return []
        if "phone" in changed:
            _check_duplicate_phone(conn, changed["phone"], exclude_id=customer_id)
        assignments = ", ".join(f"{key} = ?" for key in changed)  # keys come from the fixed whitelist above
        conn.execute(
            f"UPDATE customers SET {assignments}, updated_at = ? WHERE id = ?",
            (*changed.values(), _now(), customer_id),
        )
        log_event(conn, "customer_updated", user_id=user_id, entity="customer", entity_id=customer_id,
                  after={"fields": sorted(changed)})
        return sorted(changed)


def set_customer_active(conn, customer_id, active: bool, *, user_id) -> None:
    """Archive (False) or bring back (True) a customer. Archived customers cannot be billed."""
    require_permission(conn, user_id, "edit_customers", "archive customers")
    with transaction(conn):
        row = _row(conn, customer_id)
        _editable(row)
        if bool(row["is_active"]) == bool(active):
            return
        if active and not row["consent_given"]:
            raise ValueError("Record the customer's consent again before reactivating the profile.")
        conn.execute("UPDATE customers SET is_active = ?, updated_at = ? WHERE id = ?",
                     (1 if active else 0, _now(), customer_id))
        log_event(conn, "customer_reactivated" if active else "customer_archived",
                  user_id=user_id, entity="customer", entity_id=customer_id)


def record_consent(conn, customer_id, *, user_id, purpose=None) -> None:
    """Record (again) that the customer agreed. Also reactivates an archived profile."""
    require_permission(conn, user_id, "edit_customers", "record customer consent")
    with transaction(conn):
        row = _row(conn, customer_id)
        _editable(row)
        text = _text(purpose) or row["consent_purpose"] or get_setting(conn, "customer_consent_purpose") \
            or DEFAULT_CONSENT_PURPOSE
        now = _now()
        conn.execute(
            "UPDATE customers SET consent_given = 1, consent_date = ?, consent_purpose = ?, "
            "is_active = 1, updated_at = ? WHERE id = ?", (now, text, now, customer_id),
        )
        log_event(conn, "customer_consent_recorded", user_id=user_id, entity="customer", entity_id=customer_id)


def withdraw_consent(conn, customer_id, *, user_id) -> None:
    """The customer withdrew consent: the profile is archived so it cannot be used on new bills.

    The owner can then erase the personal data with `erase_customer`.
    """
    require_permission(conn, user_id, "edit_customers", "withdraw customer consent")
    with transaction(conn):
        row = _row(conn, customer_id)
        _editable(row)
        if not row["consent_given"] and not row["is_active"]:
            return
        now = _now()
        conn.execute(
            "UPDATE customers SET consent_given = 0, consent_date = ?, is_active = 0, updated_at = ? WHERE id = ?",
            (now, now, customer_id),
        )
        log_event(conn, "customer_consent_withdrawn", user_id=user_id, entity="customer", entity_id=customer_id)


# ---------------------------------------------------------------- finding and reading

def search_customers(conn, query, *, user_id, include_inactive=False, limit=30) -> list:
    """Find customers by part of the name or phone number (newest first when the query is empty).

    Only id, name, phone and status are returned, and searches are not written to the
    access log (opening a profile, its history or an export is).
    """
    require_permission(conn, user_id, "view_customers", "view customers")
    text = (query or "").strip()
    where, params = ["anonymised_at IS NULL"], []
    if not include_inactive:
        where.append("is_active = 1")
    if text:
        digits = _phone_digits(text)
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses, params = ["name LIKE ? ESCAPE '\\'"], [f"%{escaped}%"]
        if len(digits) >= 3:
            clauses.append("phone LIKE ?")
            params.append(f"%{digits}%")
        where.append("(" + " OR ".join(clauses) + ")")
    order = "name IS NULL, name COLLATE NOCASE, id DESC" if text else "id DESC"
    rows = conn.execute(
        f"SELECT id, name, phone, is_active FROM customers WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
        (*params, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def get_customer(conn, customer_id, *, user_id) -> dict:
    """The full profile plus store credit balance. Opening it is written to the access log."""
    require_permission(conn, user_id, "view_customers", "view customers")
    with transaction(conn):
        row = _row(conn, customer_id)
        _log_access(conn, customer_id, user_id, "view")
        data = dict(row)
    data["store_credit_paise"] = get_store_credit_balance(conn, customer_id)
    return data


def customer_history(conn, customer_id, *, user_id) -> dict:
    """Previous bills, credit notes, store credit and what the customer still owes."""
    require_permission(conn, user_id, "view_customers", "view customers")
    with transaction(conn):
        row = _row(conn, customer_id)
        _log_access(conn, customer_id, user_id, "history")
        bills, returns, outstanding = _bills_and_returns(conn, customer_id)
    return {
        "customer": {"id": row["id"], "name": row["name"], "phone": row["phone"], "is_active": bool(row["is_active"])},
        "bills": bills,
        "returns": returns,
        "store_credit_paise": get_store_credit_balance(conn, customer_id),
        "outstanding_paise": outstanding,
        "credit_sales_enabled": get_bool(conn, "credit_sales_enabled"),
    }


# ---------------------------------------------------------------- the customer's data rights

def export_customer_data(conn, customer_id, *, user_id) -> dict:
    """Everything stored about one customer, ready to give to them (JSON-friendly)."""
    require_permission(conn, user_id, "manage_customer_data", "export customer data")
    with transaction(conn):
        row = _row(conn, customer_id)
        _log_access(conn, customer_id, user_id, "export")
        log_event(conn, "customer_data_exported", user_id=user_id, entity="customer", entity_id=customer_id)
        bills, returns, _ = _bills_and_returns(conn, customer_id)
        for bill in bills:
            bill["payments"] = [
                {"mode": p["mode"], "amount_paise": p["amount_paise"], "at": p["created_at"]}
                for p in conn.execute(
                    "SELECT mode, amount_paise, created_at FROM payments "
                    "WHERE document_type = 'sale' AND document_id = ? ORDER BY id", (bill["sale_id"],))
            ]
        credit = [
            {"amount_paise": r["amount_paise"], "document": r["doc_type"], "note": r["note"], "at": r["created_at"]}
            for r in conn.execute(
                "SELECT amount_paise, doc_type, note, created_at FROM store_credit_ledger "
                "WHERE customer_id = ? ORDER BY id", (customer_id,))
        ]
        access = _access_entries(conn, customer_id)
    return {
        "exported_at": _now(),
        "customer": dict(row),
        "bills": bills,
        "credit_notes": returns,
        "store_credit_ledger": credit,
        "access_log": access,
    }


def export_customer_data_to_file(conn, customer_id, path, *, user_id):
    """Write the export as a JSON file (it holds personal data: hand it only to that customer)."""
    data = export_customer_data(conn, customer_id, user_id=user_id)
    temp = f"{path}.part"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)  # a half-written file never sits under the real name
    return path


def erase_customer(conn, customer_id, *, user_id, reason) -> dict:
    """Erase a customer's personal data on request, keeping only what bills and tax records need.

    The row stays (bills and credit notes point to it) but loses phone, address, birthday,
    size, notes and consent text, and is archived. If the customer has a GSTIN and has
    been billed, the name and GSTIN are kept because a tax invoice must show them: the
    accountant must confirm this rule. Refused while the customer still has store credit,
    an unpaid balance or a held bill, so no money claim disappears silently.

    reason must be one of ERASE_REASONS (no free text goes into the permanent audit log).
    """
    if reason not in ERASE_REASONS:
        raise ValueError("Reason must be one of: " + ", ".join(ERASE_REASONS) + ".")
    require_permission(conn, user_id, "manage_customer_data", "erase customer data")
    with transaction(conn):
        row = _row(conn, customer_id)
        if row["anonymised_at"] is not None:
            raise ValueError("This customer's personal data was already erased.")
        credit = get_store_credit_balance(conn, customer_id)
        if credit != 0:
            raise ValueError(f"The customer still has store credit of {_rs(credit)}. Settle it first.")
        _, _, outstanding = _bills_and_returns(conn, customer_id)
        if outstanding > 0:
            raise ValueError(f"The customer still owes {_rs(outstanding)}. Record the payment first.")
        if conn.execute("SELECT 1 FROM held_bills WHERE customer_id = ? LIMIT 1", (customer_id,)).fetchone():
            raise ValueError("A held bill uses this customer. Resume or discard it first.")

        billed = conn.execute("SELECT 1 FROM sales WHERE customer_id = ? LIMIT 1", (customer_id,)).fetchone()
        keep_for_tax = bool(row["gstin"] and billed)
        now = _now()
        conn.execute(
            "UPDATE customers SET name = ?, gstin = ?, phone = '', address = NULL, birthday = NULL, "
            "preferred_size = NULL, notes = NULL, consent_given = 0, consent_date = NULL, consent_purpose = NULL, "
            "is_active = 0, anonymised_at = ?, updated_at = ? WHERE id = ?",
            (row["name"] if keep_for_tax else "Erased customer", row["gstin"] if keep_for_tax else None,
             now, now, customer_id),
        )
        _log_access(conn, customer_id, user_id, "erase")
        log_event(conn, "customer_erased", user_id=user_id, entity="customer", entity_id=customer_id,
                  after={"reason": reason, "kept_for_tax": keep_for_tax})
    return {
        "customer_id": customer_id,
        "kept_for_tax": ["name", "gstin"] if keep_for_tax else [],
        "erased": ["phone", "address", "birthday", "preferred_size", "notes", "consent"]
        + ([] if keep_for_tax else ["name", "gstin"]),
    }


def _access_entries(conn, customer_id) -> list:
    return [
        {"action": r["action"], "user": r["username"], "at": r["created_at"]}
        for r in conn.execute(
            "SELECT a.action, u.username, a.created_at FROM customer_access_log a "
            "LEFT JOIN users u ON u.id = a.user_id WHERE a.customer_id = ? ORDER BY a.id", (customer_id,))
    ]


def list_access_log(conn, customer_id, *, user_id) -> list:
    """Who opened, exported or erased this customer's data, and when."""
    require_permission(conn, user_id, "manage_customer_data", "see who accessed customer data")
    _row(conn, customer_id)
    return _access_entries(conn, customer_id)


# ---------------------------------------------------------------- reviews and lists

def _months_ago(today: date, months: int) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    month += 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def inactive_customers(conn, *, user_id, months=None, today=None) -> list:
    """Customers with no bill, credit note or store-credit activity for `months` (oldest first).

    The default comes from the setting customer_retention_months (24). This only lists
    them for the owner to review; nothing is changed or erased automatically.
    """
    require_permission(conn, user_id, "manage_customer_data", "review inactive customers")
    if months is None:
        months = int(get_setting(conn, "customer_retention_months", DEFAULT_RETENTION_MONTHS))
    if months < 1:
        raise ValueError("Months must be at least 1.")
    cutoff = _months_ago(today or date.today(), months).isoformat()
    rows = conn.execute(
        "SELECT c.id, c.name, c.phone, c.is_active, MAX(c.created_at, "
        " COALESCE((SELECT MAX(created_at) FROM sales WHERE customer_id = c.id), ''), "
        " COALESCE((SELECT MAX(created_at) FROM sale_returns WHERE customer_id = c.id), ''), "
        " COALESCE((SELECT MAX(created_at) FROM store_credit_ledger WHERE customer_id = c.id), '')) AS last_activity "
        "FROM customers c WHERE c.anonymised_at IS NULL"
    ).fetchall()
    out = [
        {"id": r["id"], "name": r["name"], "phone": r["phone"], "is_active": bool(r["is_active"]),
         "last_activity": r["last_activity"], "store_credit_paise": get_store_credit_balance(conn, r["id"])}
        for r in rows if r["last_activity"][:10] < cutoff
    ]
    return sorted(out, key=lambda item: item["last_activity"])


def upcoming_birthdays(conn, *, user_id, days=7, today=None) -> list:
    """Active customers whose birthday falls in the next `days` days (a list to call; nothing is sent)."""
    require_permission(conn, user_id, "view_customers", "view customers")
    today = today or date.today()
    out = []
    for r in conn.execute(
        "SELECT id, name, phone, birthday FROM customers "
        "WHERE is_active = 1 AND anonymised_at IS NULL AND birthday IS NOT NULL"
    ):
        month, day = int(r["birthday"][-5:-3]), int(r["birthday"][-2:])
        for year in (today.year, today.year + 1):
            # 29 Feb is celebrated on 28 Feb in years that have no 29 Feb
            when = date(year, month, 28 if (month, day) == (2, 29) and not calendar.isleap(year) else day)
            if when >= today:
                break
        if (when - today).days <= days:
            out.append({"id": r["id"], "name": r["name"], "phone": r["phone"],
                        "birthday": r["birthday"][-5:], "days_until": (when - today).days})
    return sorted(out, key=lambda item: (item["days_until"], item["name"] or ""))


class CustomerEngine:
    """Compatibility wrapper for UI views."""
    def __init__(self, *args, **kwargs):
        pass 







