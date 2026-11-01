"""Purchase returns (debit notes): goods sent back to a supplier.

One call, `process_purchase_return`, is ONE transaction: the debit note number,
its lines, the stock reduction, the refund payment and the audit entry are saved
together or not at all.

Rules enforced here:
  * Only a CONFIRMED purchase can be returned against.
  * A line can be returned up to (quantity bought - quantity already returned),
    and never more than the sellable stock still in the shop.
  * Value = what we paid for those pieces: unit cost + that part of the line tax,
    minus the same share of the invoice discount (freight is not returned).
    Rounded to the nearest rupee; the round-off is kept as its own figure.
  * The value first reduces what we still owe the supplier on that purchase.
    The rest is either refunded by the supplier (`settlement='refund'`) or kept
    as a credit with the supplier (`settlement='credit'`).
  * Money is never deleted. A refund from the supplier is a NEGATIVE payment row on
    'purchase_return' (opposite of the positive 'purchase' payment that went out).
  * Debit notes are immutable (database triggers).

All money is integer paise.
"""

from datetime import date

from core.audit import log_event
from core.numbering import financial_year, next_number
from core.returns import _round_rupee, _share
from core.roles import require_role
from core.sales import ACCEPTED_PAYMENT_MODES, _on
from core.stock import InsufficientStockError, add_movement, get_stock
from db.connection import transaction

DEBIT_NOTE_SERIES = "DN"
RETURN_ROLES = ("owner", "stock")
SETTLEMENTS = ("refund", "credit")


def _returned_qty(conn, purchase_line_id) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM purchase_return_lines WHERE purchase_line_id = ?",
        (purchase_line_id,),
    ).fetchone()[0]


def _due_remaining(conn, purchase) -> int:
    """What we still owe the supplier on this purchase (never below zero)."""
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE document_type = 'purchase' AND document_id = ?",
        (purchase["id"],),
    ).fetchone()[0]
    reduced = conn.execute(
        "SELECT COALESCE(SUM(due_reduced_paise), 0) FROM purchase_returns WHERE purchase_id = ?",
        (purchase["id"],),
    ).fetchone()[0]
    return max(purchase["total_paise"] - paid - reduced, 0)


def _default_refund_mode(conn, purchase_id) -> str:
    row = conn.execute(
        "SELECT mode FROM payments WHERE document_type = 'purchase' AND document_id = ? "
        "AND mode IN ('cash','upi','card','bank_transfer','cheque','other') "
        "GROUP BY mode ORDER BY SUM(amount_paise) DESC, MIN(id) LIMIT 1",
        (purchase_id,),
    ).fetchone()
    return row["mode"] if row else "cash"


def returnable_purchase_lines(conn, purchase_id) -> list:
    """Lines of a purchase with how many pieces can still go back (for the return screen)."""
    rows = conn.execute(
        "SELECT pl.id AS purchase_line_id, pl.variant_id, pl.qty AS qty_bought, pl.unit_cost_paise, "
        "p.name AS product_name, v.size, v.colour "
        "FROM purchase_lines pl JOIN variants v ON v.id = pl.variant_id "
        "JOIN products p ON p.id = v.product_id WHERE pl.purchase_id = ? ORDER BY pl.id",
        (purchase_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["qty_returned"] = _returned_qty(conn, r["purchase_line_id"])
        d["qty_returnable"] = r["qty_bought"] - d["qty_returned"]
        d["sellable_now"] = get_stock(conn, r["variant_id"])
        result.append(d)
    return result


def process_purchase_return(
    conn,
    purchase_id,
    items,
    *,
    user_id,
    reason,
    settlement="refund",
    refund_mode=None,
    idempotency_key=None,
    on_date=None,
) -> int:
    """Send items back to the supplier. Returns the debit note id.

    items: list of {"purchase_line_id", "qty", optional "reason"}.
    Raises ValueError / InsufficientStockError and saves NOTHING when anything is wrong.
    """
    if settlement not in SETTLEMENTS:
        raise ValueError("Settlement must be 'refund' or 'credit'.")
    if not (reason or "").strip():
        raise ValueError("A reason is required for a purchase return.")
    on = _on(on_date)
    key = (idempotency_key or "").strip() or None

    with transaction(conn):
        if key:
            existing = conn.execute(
                "SELECT id FROM purchase_returns WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                return existing["id"]

        require_role(conn, user_id, RETURN_ROLES, "return goods to a supplier")
        purchase = conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
        if purchase is None:
            raise ValueError("Purchase not found.")
        if purchase["status"] != "confirmed":
            raise ValueError("Only a confirmed purchase can be returned.")
        if not isinstance(items, (list, tuple)) or not items:
            raise ValueError("Select at least one item to return.")

        # ---- work out every returned line ----
        seen, lines = set(), []
        sub_total = tax_total = 0
        for item in items:
            line_id, qty = item.get("purchase_line_id"), item.get("qty")
            if line_id in seen:
                raise ValueError("The same purchase line is listed twice. Combine the quantities.")
            seen.add(line_id)
            if not isinstance(qty, int) or qty <= 0:
                raise ValueError("Return quantity must be a whole number greater than zero.")
            row = conn.execute(
                "SELECT pl.*, p.name AS product_name, v.size, v.colour FROM purchase_lines pl "
                "JOIN variants v ON v.id = pl.variant_id JOIN products p ON p.id = v.product_id "
                "WHERE pl.id = ? AND pl.purchase_id = ?",
                (line_id, purchase_id),
            ).fetchone()
            if row is None:
                raise ValueError("A selected item is not on this purchase.")
            label = f"{row['product_name']} {row['size']} {row['colour']}"
            already = _returned_qty(conn, line_id)
            if qty > row["qty"] - already:
                raise ValueError(
                    f"Cannot return {qty} x {label}: only {row['qty'] - already} can still be returned."
                )
            have = get_stock(conn, row["variant_id"])
            if qty > have:
                raise InsufficientStockError(
                    f"Cannot return {qty} x {label}: only {have} sellable in stock."
                )
            tax = _share(row["tax_paise"], row["qty"], already + qty) - _share(row["tax_paise"], row["qty"], already)
            sub = qty * row["unit_cost_paise"]
            sub_total += sub
            tax_total += tax
            lines.append({
                "row": row, "qty": qty, "tax_paise": tax, "line_total_paise": sub + tax,
                "reason": (item.get("reason") or "").strip() or reason.strip(),
            })

        # ---- discount share (cumulative, so full return gives back exactly the whole discount) ----
        prior_sub = conn.execute(
            "SELECT COALESCE(SUM(subtotal_paise), 0) FROM purchase_returns WHERE purchase_id = ?",
            (purchase_id,),
        ).fetchone()[0]
        if purchase["subtotal_paise"] > 0 and purchase["discount_paise"] > 0:
            discount = _share(purchase["discount_paise"], purchase["subtotal_paise"], prior_sub + sub_total) \
                - _share(purchase["discount_paise"], purchase["subtotal_paise"], prior_sub)
        else:
            discount = 0

        before_round = sub_total + tax_total - discount
        total = _round_rupee(before_round)
        round_off = total - before_round

        # ---- settlement ----
        due_reduced = min(total, _due_remaining(conn, purchase))
        rest = total - due_reduced
        refund = rest if settlement == "refund" else 0
        supplier_credit = rest if settlement == "credit" else 0
        mode = refund_mode or _default_refund_mode(conn, purchase_id)
        if refund > 0 and mode not in ACCEPTED_PAYMENT_MODES:
            raise ValueError("Choose a valid payment method for the refund.")

        # ---- save ----
        fin_year = financial_year(on)
        number, debit_note_no = next_number(conn, DEBIT_NOTE_SERIES, fin_year, DEBIT_NOTE_SERIES)
        cur = conn.execute(
            "INSERT INTO purchase_returns(series, number, debit_note_no, fin_year, purchase_id, supplier_id, "
            "user_id, reason, subtotal_paise, discount_paise, tax_paise, round_off_paise, total_paise, "
            "due_reduced_paise, refund_paise, supplier_credit_paise, idempotency_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                DEBIT_NOTE_SERIES, number, debit_note_no, fin_year, purchase_id, purchase["supplier_id"],
                user_id, reason.strip(), sub_total, discount, tax_total, round_off, total,
                due_reduced, refund, supplier_credit, key,
            ),
        )
        return_id = cur.lastrowid
        for l in lines:
            row = l["row"]
            conn.execute(
                "INSERT INTO purchase_return_lines(return_id, purchase_line_id, variant_id, qty, "
                "unit_cost_paise, tax_paise, line_total_paise, reason) VALUES (?,?,?,?,?,?,?,?)",
                (return_id, row["id"], row["variant_id"], l["qty"], row["unit_cost_paise"],
                 l["tax_paise"], l["line_total_paise"], l["reason"]),
            )
            add_movement(
                conn, row["variant_id"], -l["qty"], "purchase return",
                doc_type="purchase_return", doc_id=return_id, user_id=user_id,
            )
        if refund > 0:
            conn.execute(
                "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, reason, user_id) "
                "VALUES ('purchase_return', ?, ?, ?, ?, ?, ?)",
                (return_id, mode, -refund, debit_note_no, reason.strip(), user_id),
            )
        log_event(
            conn, "purchase_return_created", user_id=user_id, entity="purchase_return", entity_id=return_id,
            after={
                "debit_note_no": debit_note_no, "voucher_no": purchase["voucher_no"], "total_paise": total,
                "due_reduced_paise": due_reduced, "refund_paise": refund, "supplier_credit_paise": supplier_credit,
            },
        )
        return return_id


def get_purchase_return(conn, return_id) -> dict:
    """A debit note with its lines and refund payment."""
    head = conn.execute("SELECT * FROM purchase_returns WHERE id = ?", (return_id,)).fetchone()
    if head is None:
        raise ValueError("Purchase return not found.")
    lines = conn.execute(
        "SELECT * FROM purchase_return_lines WHERE return_id = ? ORDER BY id", (return_id,)
    ).fetchall()
    payments = conn.execute(
        "SELECT * FROM payments WHERE document_type = 'purchase_return' AND document_id = ? ORDER BY id",
        (return_id,),
    ).fetchall()
    return {"return": dict(head), "lines": [dict(l) for l in lines], "payments": [dict(p) for p in payments]}


def list_purchase_returns(conn, purchase_id) -> list:
    return conn.execute(
        "SELECT * FROM purchase_returns WHERE purchase_id = ? ORDER BY id", (purchase_id,)
    ).fetchall()
