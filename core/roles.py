"""Small role check used by the stock and purchase-return code.

Step 9 (login and permissions) will replace this with the full permission
system. Until then the rule is enforced here, in the data layer, so it cannot
be bypassed by a screen.
"""


def require_role(conn, user_id, roles, action: str):
    """Return the user row if the user is active and has one of `roles`, else raise ValueError."""
    row = conn.execute("SELECT id, role FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
    if row is None:
        raise ValueError("User not found or not active.")
    if row["role"] not in roles:
        raise ValueError(f"Your role is not allowed to {action}.")
    return row
