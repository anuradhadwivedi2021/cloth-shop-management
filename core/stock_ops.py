"""Damaged, missing and returned stock.

Every action is ONE transaction: the adjustment record, the stock movement(s)
and the audit entry are saved together or not at all. Nothing is deleted.

  mark_damaged     sellable        -> damaged bucket
  mark_missing     sellable        -> gone (recorded as a 'missing' adjustment)
  review_returned  returned_pending-> sellable (fit for resale) or damaged

Only the owner or stock staff may do these. Damaged and returned-pending
pieces are NOT sellable, so billing never sees them.
"""

from core.audit import log_event
from core.roles import require_role
from core.stock import add_movement
from db.connection import transaction

STOCK_ROLES = ("owner", "stock")


def _check(conn, variant_id, qty, reason, user_id, action):
    require_role(conn, user_id, STOCK_ROLES, action)
    if not isinstance(qty, int) or qty <= 0:
        raise ValueError("Quantity must be a whole number greater than zero.")
    if not (reason or "").strip():
        raise ValueError("A reason is required.")
    if conn.execute("SELECT 1 FROM variants WHERE id = ?", (variant_id,)).fetchone() is None:
        raise ValueError("Product variant not found.")


def _record(conn, kind, variant_id, qty, reason, user_id, from_bucket, to_bucket):
    reason = reason.strip()
    cur = conn.execute(
        "INSERT INTO stock_adjustments(kind, variant_id, qty, reason, user_id) VALUES (?,?,?,?,?)",
        (kind, variant_id, qty, reason, user_id),
    )
    adj_id = cur.lastrowid
    text = f"{kind.replace('_', ' ')}: {reason}"
    add_movement(
        conn, variant_id, -qty, text, bucket=from_bucket,
        doc_type="stock_adjustment", doc_id=adj_id, user_id=user_id,
    )
    if to_bucket:
        add_movement(
            conn, variant_id, qty, text, bucket=to_bucket,
            doc_type="stock_adjustment", doc_id=adj_id, user_id=user_id,
        )
    log_event(
        conn, f"stock_{kind}", user_id=user_id, entity="stock_adjustment", entity_id=adj_id,
        after={"variant_id": variant_id, "qty": qty, "reason": reason, "from": from_bucket, "to": to_bucket},
    )
    return adj_id


def mark_damaged(conn, variant_id, qty, reason, *, user_id) -> int:
    """Move sellable pieces to the damaged bucket. Returns the adjustment id."""
    with transaction(conn):
        _check(conn, variant_id, qty, reason, user_id, "record damaged stock")
        return _record(conn, "damaged", variant_id, qty, reason, user_id, "sellable", "damaged")


def mark_missing(conn, variant_id, qty, reason, *, user_id) -> int:
    """Take lost / missing pieces out of sellable stock. Returns the adjustment id."""
    with transaction(conn):
        _check(conn, variant_id, qty, reason, user_id, "record missing stock")
        return _record(conn, "missing", variant_id, qty, reason, user_id, "sellable", None)


def review_returned(conn, variant_id, qty, *, resellable: bool, user_id, reason=None) -> int:
    """Check returned pieces: fit for resale -> sellable, otherwise -> damaged."""
    kind = "returned_ok" if resellable else "returned_damaged"
    text = (reason or "").strip() or ("checked, fit for resale" if resellable else "checked, not fit for resale")
    with transaction(conn):
        _check(conn, variant_id, qty, text, user_id, "check returned items")
        return _record(
            conn, kind, variant_id, qty, text, user_id,
            "returned_pending", "sellable" if resellable else "damaged",
        )


def list_adjustments(conn, variant_id=None, kind=None) -> list:
    """Adjustment history, newest first."""
    sql, args = "SELECT * FROM stock_adjustments WHERE 1=1", []
    if variant_id is not None:
        sql += " AND variant_id = ?"
        args.append(variant_id)
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    return conn.execute(sql + " ORDER BY id DESC", args).fetchall()
