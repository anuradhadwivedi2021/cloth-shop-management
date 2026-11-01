"""Counter actions that enforce permissions, discount limits and the owner PIN.

The screens must call these (not core.sales / core.returns directly), so the
rules hold even if a screen forgets to hide a button.

  sell          needs 'sell'. A discount needs 'discount' and must be within the
                user's limit; above it the owner's PIN is required.
  return_items  needs 'process_return'. If the return policy blocks the return,
                the owner's PIN (or the owner signed in) can override it with a reason.
"""

from core.audit import log_event
from core.auth import verify_owner_pin
from core.permissions import PermissionDenied, discount_cap_percent, has_permission, require_permission
from core.returns import process_return
from core.sales import save_sale_in_transaction
from db.connection import transaction


def _gross_and_discount(conn, cart, bill_discount_paise):
    gross = discount = 0
    for item in cart:
        row = conn.execute(
            "SELECT v.price_override_paise, p.default_price_paise FROM variants v "
            "JOIN products p ON p.id = v.product_id WHERE v.id = ?",
            (item.get("variant_id"),),
        ).fetchone()
        if row is None or not isinstance(item.get("qty"), int):
            continue  # the sale itself will reject this line with a clear message
        price = row["price_override_paise"] if row["price_override_paise"] is not None else row["default_price_paise"]
        gross += price * item["qty"]
        discount += item.get("discount_paise", 0) or 0
    return gross, discount + (bill_discount_paise or 0)


def sell(conn, cart, payments, *, user_id, approver_pin=None, **kwargs) -> int:
    """Save a bill with permission and discount-limit checks. Returns the sale id."""
    require_permission(conn, user_id, "sell", "make a sale")
    key = (kwargs.get("idempotency_key") or "").strip()
    if key:
        existing = conn.execute("SELECT id FROM sales WHERE idempotency_key = ?", (key,)).fetchone()
        if existing:
            return existing["id"]

    approver_id = None
    if isinstance(cart, (list, tuple)):
        gross, discount = _gross_and_discount(conn, cart, kwargs.get("bill_discount_paise", 0))
        if discount > 0:
            require_permission(conn, user_id, "discount", "give a discount")
            percent = discount * 100 / gross if gross else 100.0
            cap = discount_cap_percent(conn, user_id)
            if percent > cap + 1e-9:
                if not approver_pin:
                    raise PermissionDenied(
                        f"Discount of {percent:.1f}% is above your limit of {cap:g}%. The owner's PIN is needed."
                    )
                approver_id = verify_owner_pin(conn, approver_pin)  # wrong PINs are recorded and throttled

    with transaction(conn):
        sale_id = save_sale_in_transaction(conn, cart, payments, user_id=user_id, **kwargs)
        if approver_id is not None:
            log_event(
                conn, "discount_override", user_id=user_id, entity="sale", entity_id=sale_id,
                after={"approved_by": approver_id, "discount_percent": round(percent, 2), "limit_percent": cap},
            )
        return sale_id


def return_items(conn, sale_id, items, *, user_id, approver_pin=None, override_reason=None, **kwargs) -> int:
    """Process a return / exchange with permission checks. Returns the credit note id."""
    require_permission(conn, user_id, "process_return", "process returns")
    override_id = None
    if approver_pin:
        override_id = verify_owner_pin(conn, approver_pin)
    elif has_permission(conn, user_id, "override_return_policy"):
        override_id = user_id  # the owner is signed in and may override with a reason
    return process_return(
        conn, sale_id, items, user_id=user_id,
        override_user_id=override_id, override_reason=override_reason, **kwargs,
    )
