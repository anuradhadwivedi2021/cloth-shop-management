"""User administration for the Users screen. No screen code here, so it can be tested.

Everything goes through the tested functions in core.auth / core.permissions
(hashing, permission check, one transaction, audit log). The screen never touches SQL.

Extra rule on top of core.auth: only an owner may create an owner, or change, unlock,
disable or reset the password of an owner account. Otherwise a cashier who was given
"manage users" could take over the owner's login.
"""

from datetime import datetime

from core import auth
from core.permissions import PermissionDenied, require_permission

ROLE_LABELS = {"owner": "Owner", "cashier": "Cashier", "stock": "Stock staff", "accountant": "Accountant"}
_LABEL_TO_ROLE = {label.lower(): role for role, label in ROLE_LABELS.items()}


def role_key(role_or_label) -> str:
    """'Stock staff' -> 'stock'; 'cashier' -> 'cashier'. Unknown values are refused."""
    text = (role_or_label or "").strip().lower()
    key = _LABEL_TO_ROLE.get(text, text)
    if key not in auth.ROLES:
        raise ValueError("Please choose a valid role.")
    return key


def _role_of(conn, user_id):
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else row["role"]


def _only_owner_touches_owner(conn, admin_id, target_id) -> None:
    target = _role_of(conn, target_id)
    if target is None:
        raise ValueError("User not found.")
    if target == "owner" and _role_of(conn, admin_id) != "owner":
        raise PermissionDenied("Only an owner can change an owner account.")


def list_users(conn, admin_id, *, now=None) -> list:
    """All users for the table. Never includes password or PIN data."""
    require_permission(conn, admin_id, "manage_users", "see the user list")
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT id, username, full_name, role, is_active, locked_until, pin_hash IS NOT NULL AS has_pin "
        "FROM users ORDER BY id"
    ).fetchall()
    users = []
    for r in rows:
        locked = auth._remaining(r["locked_until"], now) > 0
        users.append({
            "id": r["id"],
            "username": r["username"],
            "full_name": r["full_name"] or "",
            "role": r["role"],
            "role_label": ROLE_LABELS[r["role"]],
            "is_active": bool(r["is_active"]),
            "locked": locked,
            "has_pin": bool(r["has_pin"]),
            "status": "Disabled" if not r["is_active"] else ("Locked" if locked else "Active"),
        })
    return users


def add_user(conn, admin_id, username, password, role, *, pin=None, full_name=None) -> int:
    """Create a NEW user. An existing username is refused, never overwritten."""
    key = role_key(role)
    require_permission(conn, admin_id, "manage_users", "add users")
    if key == "owner" and _role_of(conn, admin_id) != "owner":
        raise PermissionDenied("Only an owner can create another owner.")
    return auth.create_user(conn, admin_id, username, password, key, pin=(pin or None), full_name=full_name)


def reset_password(conn, admin_id, user_id, new_password) -> None:
    require_permission(conn, admin_id, "manage_users", "reset passwords")
    _only_owner_touches_owner(conn, admin_id, user_id)
    auth.reset_password(conn, admin_id, user_id, new_password)


def set_active(conn, admin_id, user_id, active: bool) -> None:
    require_permission(conn, admin_id, "manage_users", "change user status")
    _only_owner_touches_owner(conn, admin_id, user_id)
    auth.set_user_active(conn, admin_id, user_id, active)


def unlock(conn, admin_id, user_id) -> None:
    require_permission(conn, admin_id, "manage_users", "unlock accounts")
    _only_owner_touches_owner(conn, admin_id, user_id)
    auth.unlock_account(conn, admin_id, user_id)
