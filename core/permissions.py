"""Who may do what. Enforced here, in the data layer, not only by hiding buttons.

Each role has default permissions. The owner can grant or take away single
permissions for a single user (user_permissions table). The owner always has
everything and cannot be restricted, so the shop can never lock itself out.
"""

from core.audit import log_event
from db.connection import transaction

ALL_PERMISSIONS = {
    "sell": "Create bills",
    "hold_bills": "Hold and resume bills",
    "discount": "Give discounts (up to the user's limit)",
    "process_return": "Process customer returns and exchanges",
    "override_return_policy": "Override the return policy",
    "change_price": "Change prices",
    "view_cost": "See purchase cost and margins",
    "view_products": "See products and stock",
    "edit_products": "Add and edit products and variants",
    "view_customers": "See customer details",
    "edit_customers": "Create and edit customers",
    "manage_customer_data": "Export, erase and review customer personal data",
    "receive_purchase": "Record and confirm purchases",
    "return_to_supplier": "Return goods to a supplier",
    "adjust_stock": "Count stock, record damaged or missing items",
    "approve_stock_count": "Approve a stock count",
    "print_labels": "Print barcode labels",
    "view_reports": "See reports",
    "export_reports": "Export reports",
    "manage_cash": "Cash register and payments",
    "manage_expenses": "Record expenses",
    "create_backup": "Create a backup",
    "restore_backup": "Restore a backup",
    "manage_users": "Manage users and permissions",
    "manage_settings": "Change shop settings",
}

ROLE_DEFAULTS = {
    "owner": frozenset(ALL_PERMISSIONS),
    "cashier": frozenset({
        "sell", "hold_bills", "discount", "process_return",
        "view_products", "view_customers", "edit_customers",
    }),
    "stock": frozenset({
        "receive_purchase", "return_to_supplier", "adjust_stock", "print_labels", "view_products",
        "edit_products",
    }),
    "accountant": frozenset({
        "view_reports", "export_reports", "view_cost", "view_products", "view_customers",
    }),
}

# Largest discount (percent of the bill before discount) a user may give without the owner's PIN.
DEFAULT_DISCOUNT_CAP = {"owner": 100.0, "cashier": 10.0, "stock": 0.0, "accountant": 0.0}


class PermissionDenied(ValueError):
    """The user is not allowed to do this."""


def _active_user(conn, user_id):
    row = conn.execute("SELECT id, role FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
    if row is None:
        raise PermissionDenied("User not found or not active.")
    return row


def user_permissions(conn, user_id) -> set:
    """All permissions the user has right now (role default plus the owner's grants and removals)."""
    user = _active_user(conn, user_id)
    perms = set(ROLE_DEFAULTS[user["role"]])
    if user["role"] == "owner":
        return perms
    for row in conn.execute("SELECT permission, allowed FROM user_permissions WHERE user_id = ?", (user_id,)):
        if row["permission"] not in ALL_PERMISSIONS:
            continue
        (perms.add if row["allowed"] else perms.discard)(row["permission"])
    return perms


def has_permission(conn, user_id, permission: str) -> bool:
    try:
        return permission in user_permissions(conn, user_id)
    except PermissionDenied:
        return False


def require_permission(conn, user_id, permission: str, action=None):
    """Return the user row, or raise PermissionDenied (a ValueError)."""
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission}")
    user = _active_user(conn, user_id)
    if permission not in user_permissions(conn, user_id):
        what = action or ALL_PERMISSIONS[permission].lower()
        raise PermissionDenied(f"You are not allowed to {what}.")
    return user


def discount_cap_percent(conn, user_id) -> float:
    user = _active_user(conn, user_id)
    row = conn.execute("SELECT discount_cap_percent FROM users WHERE id = ?", (user_id,)).fetchone()
    if row["discount_cap_percent"] is not None:
        return float(row["discount_cap_percent"])
    return DEFAULT_DISCOUNT_CAP[user["role"]]


def set_user_permission(conn, admin_id, user_id, permission: str, allowed: bool) -> None:
    """Owner grants (True) or removes (False) one permission for one user."""
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission}")
    with transaction(conn):
        require_permission(conn, admin_id, "manage_users", "change user permissions")
        target = conn.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise ValueError("User not found.")
        if target["role"] == "owner":
            raise ValueError("The owner always has every permission and cannot be restricted.")
        before = conn.execute(
            "SELECT allowed FROM user_permissions WHERE user_id = ? AND permission = ?", (user_id, permission)
        ).fetchone()
        conn.execute(
            "INSERT INTO user_permissions(user_id, permission, allowed, set_by) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, permission) DO UPDATE SET allowed = excluded.allowed, "
            "set_by = excluded.set_by, updated_at = datetime('now','localtime')",
            (user_id, permission, 1 if allowed else 0, admin_id),
        )
        log_event(
            conn, "permission_changed", user_id=admin_id, entity="user", entity_id=user_id,
            before=None if before is None else {permission: bool(before["allowed"])},
            after={permission: bool(allowed)},
        )


def clear_user_permission(conn, admin_id, user_id, permission: str) -> None:
    """Remove the owner's grant/removal so the role default applies again."""
    with transaction(conn):
        require_permission(conn, admin_id, "manage_users", "change user permissions")
        conn.execute("DELETE FROM user_permissions WHERE user_id = ? AND permission = ?", (user_id, permission))
        log_event(
            conn, "permission_reset", user_id=admin_id, entity="user", entity_id=user_id,
            after={"permission": permission},
        )


def set_discount_cap(conn, admin_id, user_id, percent) -> None:
    """Owner sets a user's discount limit (None = role default)."""
    if percent is not None and not 0 <= percent <= 100:
        raise ValueError("Discount limit must be between 0 and 100 percent.")
    with transaction(conn):
        require_permission(conn, admin_id, "manage_users", "change discount limits")
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise ValueError("User not found.")
        conn.execute("UPDATE users SET discount_cap_percent = ? WHERE id = ?", (percent, user_id))
        log_event(
            conn, "discount_cap_changed", user_id=admin_id, entity="user", entity_id=user_id,
            after={"discount_cap_percent": percent},
        )