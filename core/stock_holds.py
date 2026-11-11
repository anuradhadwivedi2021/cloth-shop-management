"""Stock held for a customer: trial and reserved pieces (SRS STK-03).

  place_hold      sellable -> on_trial or reserved, for a set number of days
  release_hold    on_trial / reserved -> sellable (all of it or only some pieces)
  list_holds      what is out right now, with who has it and whether it is overdue

Held pieces are NOT sellable, so billing can never sell them. If the customer decides to buy, release the
pieces and bill them in the normal way: stock goes down once, at the bill, and nowhere else.

Every action is ONE transaction: the hold record, both stock movements and the audit entry are saved
together or not at all. Nothing is deleted. Allowed for anyone who may hold bills (cashier, owner) or
adjust stock (stock staff, owner).
"""

from datetime import date, timedelta

from core.audit import log_event
from core.permissions import PermissionDenied, has_permission, require_permission
from core.stock import add_movement
from db.connection import transaction

KINDS = ("trial", "reserved")
BUCKET_OF = {"trial": "on_trial", "reserved": "reserved"}   # the stock bucket each kind of hold lives in
MAX_HOLD_DAYS = 60
_WHO_MAY = ("hold_bills", "adjust_stock")


def _require(conn, user_id, action) -> None:
    if not any(has_permission(conn, user_id, p) for p in _WHO_MAY):
        require_permission(conn, user_id, _WHO_MAY[0], action)  # raises PermissionDenied with the standard text
        raise PermissionDenied(f"You are not allowed to {action}.")


def _today(today=None) -> date:
    return today or date.today()


def _open_qty(conn, hold_id) -> int:
    return conn.execute(
        "SELECT h.qty - COALESCE((SELECT SUM(r.qty) FROM stock_hold_releases r WHERE r.hold_id = h.id), 0) "
        "FROM stock_holds h WHERE h.id = ?", (hold_id,)).fetchone()[0]


def place_hold(conn, user_id, variant_id, qty, kind, days, *, held_for=None, note=None, today=None) -> int:
    """Take `qty` pieces out of sellable stock for `days` days. Returns the hold id.

    Refused (nothing saved) when there are not enough sellable pieces.
    """
    if kind not in KINDS:
        raise ValueError("Choose trial or reserved.")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValueError("Quantity must be a whole number greater than zero.")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_HOLD_DAYS:
        raise ValueError(f"The number of days must be between 1 and {MAX_HOLD_DAYS}.")
    with transaction(conn):
        _require(conn, user_id, "hold stock for a customer")
        if conn.execute("SELECT 1 FROM variants WHERE id = ?", (variant_id,)).fetchone() is None:
            raise ValueError("Product variant not found.")
        until = (_today(today) + timedelta(days=days)).isoformat()
        held_for = (held_for or "").strip() or None
        note = (note or "").strip() or None
        cur = conn.execute(
            "INSERT INTO stock_holds(kind, variant_id, qty, held_for, note, hold_until, user_id) VALUES (?,?,?,?,?,?,?)",
            (kind, variant_id, qty, held_for, note, until, user_id))
        hold_id = cur.lastrowid
        text = f"{kind} hold" + (f" for {held_for}" if held_for else "")
        add_movement(conn, variant_id, -qty, text, bucket="sellable", doc_type="stock_hold", doc_id=hold_id, user_id=user_id)
        add_movement(conn, variant_id, qty, text, bucket=BUCKET_OF[kind],
                     doc_type="stock_hold", doc_id=hold_id, user_id=user_id)
        log_event(conn, "stock_hold_placed", user_id=user_id, entity="stock_hold", entity_id=hold_id,
                  after={"variant_id": variant_id, "qty": qty, "kind": kind, "held_for": held_for, "until": until})
        return hold_id


def release_hold(conn, user_id, hold_id, qty=None, *, note=None) -> int:
    """Bring held pieces back to sellable stock. qty=None releases everything still out. Returns pieces released."""
    with transaction(conn):
        _require(conn, user_id, "release held stock")
        hold = conn.execute("SELECT * FROM stock_holds WHERE id = ?", (hold_id,)).fetchone()
        if hold is None:
            raise ValueError("That hold was not found.")
        open_qty = _open_qty(conn, hold_id)
        if open_qty <= 0:
            raise ValueError("All pieces of this hold are already back in stock.")
        qty = open_qty if qty is None else qty
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise ValueError("Quantity must be a whole number greater than zero.")
        if qty > open_qty:
            raise ValueError(f"Only {open_qty} piece(s) of this hold are still out.")
        note = (note or "").strip() or None
        conn.execute("INSERT INTO stock_hold_releases(hold_id, qty, note, user_id) VALUES (?,?,?,?)",
                     (hold_id, qty, note, user_id))
        bucket = BUCKET_OF[hold["kind"]]
        text = f"{hold['kind']} hold released" + (f": {note}" if note else "")
        add_movement(conn, hold["variant_id"], -qty, text, bucket=bucket, doc_type="stock_hold", doc_id=hold_id, user_id=user_id)
        add_movement(conn, hold["variant_id"], qty, text, bucket="sellable", doc_type="stock_hold", doc_id=hold_id, user_id=user_id)
        log_event(conn, "stock_hold_released", user_id=user_id, entity="stock_hold", entity_id=hold_id,
                  after={"variant_id": hold["variant_id"], "qty": qty, "note": note})
        return qty


def list_holds(conn, user_id, *, include_closed=False, today=None) -> list:
    """Holds with pieces still out (or all holds), oldest due date first, with item names and an overdue flag."""
    _require(conn, user_id, "see held stock")
    today_text = _today(today).isoformat()
    rows = conn.execute(
        "SELECT h.id, h.kind, h.variant_id, h.qty, h.held_for, h.note, h.hold_until, h.created_at, "
        "p.name AS product_name, v.size, v.colour, u.username, "
        "h.qty - COALESCE((SELECT SUM(r.qty) FROM stock_hold_releases r WHERE r.hold_id = h.id), 0) AS open_qty "
        "FROM stock_holds h JOIN variants v ON v.id = h.variant_id JOIN products p ON p.id = v.product_id "
        "LEFT JOIN users u ON u.id = h.user_id ORDER BY h.hold_until, h.id").fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if row["open_qty"] <= 0 and not include_closed:
            continue
        row["overdue"] = row["open_qty"] > 0 and row["hold_until"] < today_text
        out.append(row)
    return out
