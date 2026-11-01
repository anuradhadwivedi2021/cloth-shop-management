"""Stock movements: the ONLY way stock ever changes.

Stock is never stored as an editable number. Every change is an append-only row
in stock_movements, and the current quantity is the sum of those rows.
"""


class InsufficientStockError(Exception):
    """Raised when a movement would make sellable stock negative."""


def get_stock(conn, variant_id: int, bucket: str = "sellable") -> int:
    """Current quantity of one variant in one bucket."""
    row = conn.execute(
        "SELECT COALESCE(SUM(qty_change), 0) FROM stock_movements WHERE variant_id = ? AND bucket = ?",
        (variant_id, bucket),
    ).fetchone()
    return row[0]


def add_movement(
    conn,
    variant_id: int,
    qty_change: int,
    reason: str,
    *,
    bucket: str = "sellable",
    doc_type=None,
    doc_id=None,
    user_id=None,
    allow_negative: bool = False,
) -> int:
    """Record one stock movement. Returns the balance after it.

    Must be called inside `with transaction(conn):` so it is saved together
    with the document that caused it (purchase, sale, return, ...).
    Negative stock is blocked unless allow_negative is True (a shop setting).
    """
    if not conn.in_transaction:
        raise RuntimeError("add_movement must be called inside a transaction.")
    if not isinstance(qty_change, int) or qty_change == 0:
        raise ValueError("Quantity change must be a non-zero whole number.")
    if not (reason or "").strip():
        raise ValueError("A reason is required for every stock movement.")

    new_balance = get_stock(conn, variant_id, bucket) + qty_change
    if new_balance < 0 and not allow_negative:
        raise InsufficientStockError(
            f"Not enough stock: only {new_balance - qty_change} available in '{bucket}'."
        )

    conn.execute(
        "INSERT INTO stock_movements(variant_id, qty_change, bucket, reason, doc_type, doc_id, user_id, balance_after) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (variant_id, qty_change, bucket, reason.strip(), doc_type, doc_id, user_id, new_balance),
    )
    return new_balance