"""Suppliers for the Suppliers screen: add (checked, audited), edit and list with what we owe.

The screen never touches SQL. Adding needs "receive_purchase" (the same permission that
edits suppliers). The older core.purchases.create_supplier stays as it is for tests and
imports; screens use add_supplier here, which also checks the GSTIN, warns about a
supplier that may already exist (never merges), and writes the audit log.
"""

import re

from core.audit import log_event
from core.customers import validate_gstin
from core.permissions import require_permission
from core.supplier_ledger import all_supplier_balances, search_suppliers, update_supplier
from db.connection import transaction

_PHONE = re.compile(r"[0-9+\-() ]{6,20}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class DuplicateSupplier(ValueError):
    """A supplier with the same name, phone or GSTIN already exists. `matches` lists them."""

    def __init__(self, message, matches):
        super().__init__(message)
        self.matches = matches


def _clean(value):
    return (value or "").strip() or None


def _check_contact(phone, email) -> None:
    if phone and not _PHONE.fullmatch(phone):
        raise ValueError("Phone number looks wrong. Use digits, spaces, + - ( ) only.")
    if email and not _EMAIL.fullmatch(email):
        raise ValueError("Email address looks wrong.")


def _similar(conn, name, phone, gstin) -> list:
    found = {}
    rules = [("same name", "LOWER(name) = LOWER(?)", name)]
    if phone:
        rules.append(("same phone", "phone = ?", phone))
    if gstin:
        rules.append(("same GSTIN", "gstin = ?", gstin))
    for reason, where, value in rules:
        for row in conn.execute(f"SELECT id, name, phone, gstin, is_active FROM suppliers WHERE {where}", (value,)):
            entry = found.setdefault(row["id"], {
                "id": row["id"], "name": row["name"], "phone": row["phone"], "gstin": row["gstin"],
                "is_active": bool(row["is_active"]), "reasons": [],
            })
            entry["reasons"].append(reason)
    return list(found.values())


def add_supplier(
    conn, user_id, name, *, contact_person=None, phone=None, email=None, address=None, gstin=None, state=None,
    confirm_duplicate: bool = False,
) -> int:
    """Create a supplier. Returns its id. A look-alike is refused unless confirm_duplicate=True."""
    with transaction(conn):
        require_permission(conn, user_id, "receive_purchase", "add suppliers")
        name, phone, email = _clean(name), _clean(phone), _clean(email)
        if not name:
            raise ValueError("Supplier name is required.")
        _check_contact(phone, email)
        gstin = validate_gstin(gstin) if _clean(gstin) else None
        matches = _similar(conn, name, phone, gstin)
        if matches and not confirm_duplicate:
            raise DuplicateSupplier("A supplier like this already exists.", matches)
        cur = conn.execute(
            "INSERT INTO suppliers(name, contact_person, phone, email, address, gstin, state) VALUES (?,?,?,?,?,?,?)",
            (name, _clean(contact_person), phone, email, _clean(address), gstin, _clean(state)),
        )
        log_event(
            conn, "supplier_created", user_id=user_id, entity="supplier", entity_id=cur.lastrowid,
            after={"name": name, "phone": phone, "gstin": gstin},
        )
        return cur.lastrowid


def edit_supplier(conn, user_id, supplier_id, **fields) -> list:
    """Change details (same rules as add). Returns the names of the fields that really changed."""
    if "phone" in fields or "email" in fields:
        _check_contact(_clean(fields.get("phone")), _clean(fields.get("email")))
    return update_supplier(conn, user_id, supplier_id, **fields)


def list_suppliers(conn, user_id, *, query=None, include_inactive=False) -> list:
    """Suppliers for the table, each with balance_paise (positive = we owe them)."""
    rows = search_suppliers(conn, user_id=user_id, query=query, include_inactive=include_inactive, limit=1000)
    owed = {b["supplier_id"]: b["balance_paise"] for b in all_supplier_balances(conn, user_id=user_id, include_zero=True)}
    return [
        {
            "id": r["id"], "name": r["name"], "contact_person": r["contact_person"] or "", "phone": r["phone"] or "",
            "email": r["email"] or "", "address": r["address"] or "", "gstin": r["gstin"] or "",
            "state": r["state"] or "", "is_active": bool(r["is_active"]), "balance_paise": owed.get(r["id"], 0),
        }
        for r in rows
    ]
