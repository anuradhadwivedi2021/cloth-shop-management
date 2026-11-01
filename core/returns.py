"""Sales returns and exchanges (credit notes).

One call, `process_return`, is ONE transaction: the credit note number, the
returned lines, the stock movement, the refund / store credit and (for an
exchange) the replacement bill are saved together or not at all.

Rules enforced here:
  * A line can only be returned up to (quantity sold - quantity already returned).
    Retrying the same return therefore cannot be processed twice; passing an
    `idempotency_key` makes a double click return the credit note already saved.
  * The returned value is what the customer actually paid for those pieces
    (the original bill line, discount and tax included), never today's price.
  * Returned pieces go to the 'returned_pending' stock bucket. They are NOT
    sellable until an authorised user checks them (Step 8).
  * The return policy comes from shop settings and the owner can override it
    with a reason (the override is saved on the credit note and logged):
        return_window_days     whole number of days; empty = no time limit
        final_sale_categories  comma separated product categories that cannot be returned
  * Money is never deleted: a refund is a NEGATIVE payment row that points at the credit note.
  * Credit notes are immutable (database triggers).

All money is integer paise.
"""

from datetime import date, timedelta

from core.audit import log_event
from core.numbering import financial_year, next_number
from core.sales import ACCEPTED_PAYMENT_MODES, _on, save_sale_in_transaction
from core.settings import get_setting
from core.stock import add_movement
from db.connection import transaction

CREDIT_NOTE_SERIES = "CN"
OUTCOMES = ("refund", "store_credit", "exchange")

# Suggested reasons for the screen; any non-empty text is accepted.
RETURN_REASONS = ("wrong_size", "wrong_colour", "defect", "customer_preference", "other")


# ---------- small helpers ----------

def _share(total: int, sold: int, upto: int) -> int:
    """Part of `total` that belongs to the first `upto` of `sold` pieces (half-up, exact at upto == sold)."""
    return (2 * total * upto + sold) // (2 * sold)


def _round_rupee(paise: int) -> int:
    return ((paise + 50) // 100) * 100


def _prior_lines_total(conn, sale_id: int) -> int:
    """Value of all pieces of this bill returned so far (before rounding)."""
    return conn.execute(
        "SELECT COALESCE(SUM(l.line_total_paise), 0) FROM sale_return_lines l "
        "JOIN sale_returns r ON r.id = l.return_id WHERE r.sale_id = ?",
        (sale_id,),
    ).fetchone()[0]


def _window_days(conn):
    raw = get_setting(conn, "return_window_days")
    if raw is None or not str(raw).strip():
        return None
    try:
        days = int(str(raw).strip())
    except ValueError:
        raise ValueError("The return period setting must be a whole number of days.") from None
    if days < 0:
        raise ValueError("The return period setting cannot be negative.")
    return days


def _final_sale_categories(conn):
    raw = get_setting(conn, "final_sale_categories") or ""
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def _returned_qty(conn, sale_line_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM sale_return_lines WHERE sale_line_id = ?", (sale_line_id,)
    ).fetchone()[0]


def _sale_paid(conn, sale_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE document_type = 'sale' AND document_id = ?",
        (sale_id,),
    ).fetchone()[0]


def _due_remaining(conn, sale) -> int:
    """Amount the customer still owes on this bill after earlier returns (never below zero)."""
    adjusted = conn.execute(
        "SELECT COALESCE(SUM(due_adjusted_paise), 0) FROM sale_returns WHERE sale_id = ?", (sale["id"],)
    ).fetchone()[0]
    return max(sale["total_paise"] - _sale_paid(conn, sale["id"]) - adjusted, 0)


def _default_refund_mode(conn, sale_id: int) -> str:
    """Refund goes back the way the money came: the mode that paid most of the bill."""
    row = conn.execute(
        "SELECT mode FROM payments WHERE document_type = 'sale' AND document_id = ? "
        "AND mode IN ('cash','upi','card','bank_transfer','cheque','other') "
        "GROUP BY mode ORDER BY SUM(amount_paise) DESC, MIN(id) LIMIT 1",
        (sale_id,),
    ).fetchone()
    return row["mode"] if row else "cash"


def _active_user(conn, user_id, what="User"):
    row = conn.execute("SELECT id, role FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
    if row is None:
        raise ValueError(f"{what} not found or not active.")
    return row


# ---------- reading ----------

def returnable_lines(conn, sale_id) -> list:
    """Each line of a bill with how many pieces were sold, already returned and can still be returned."""
    if conn.execute("SELECT 1 FROM sales WHERE id = ?", (sale_id,)).fetchone() is None:
        raise ValueError("Bill not found.")
    out = []
    for row in conn.execute("SELECT * FROM sale_lines WHERE sale_id = ? ORDER BY id", (sale_id,)):
        returned = _returned_qty(conn, row["id"])
        out.append({
            "sale_line_id": row["id"],
            "variant_id": row["variant_id"],
            "product_name": row["product_name"],
            "size": row["size"],
            "colour": row["colour"],
            "qty_sold": row["qty"],
            "qty_returned": returned,
            "qty_returnable": row["qty"] - returned,
            "unit_price_paise": row["unit_price_paise"],
        })
    return out


def get_return(conn, return_id) -> dict:
    """A saved credit note with its lines, money movements and links to both bills."""
    ret = conn.execute("SELECT * FROM sale_returns WHERE id = ?", (return_id,)).fetchone()
    if ret is None:
        raise ValueError("Credit note not found.")
    ret = dict(ret)
    lines = [
        dict(r) for r in conn.execute("SELECT * FROM sale_return_lines WHERE return_id = ? ORDER BY id", (return_id,))
    ]
    payments = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM payments WHERE document_type = 'sale_return' AND document_id = ? ORDER BY id", (return_id,)
        )
    ]
    original = conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (ret["sale_id"],)).fetchone()
    exchange = None
    if ret["exchange_sale_id"] is not None:
        exchange = conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (ret["exchange_sale_id"],)).fetchone()
    return {
        "return": ret,
        "lines": lines,
        "payments": payments,
        "original_invoice_no": original["invoice_no"],
        "exchange_invoice_no": exchange["invoice_no"] if exchange else None,
    }


def list_returns_for_sale(conn, sale_id) -> list:
    return [
        dict(r) for r in conn.execute("SELECT * FROM sale_returns WHERE sale_id = ? ORDER BY id", (sale_id,))
    ]


def get_store_credit_balance(conn, customer_id) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM store_credit_ledger WHERE customer_id = ?", (customer_id,)
    ).fetchone()[0]


# ---------- processing a return or exchange ----------

def process_return(
    conn,
    sale_id,
    items,
    *,
    user_id,
    outcome="refund",
    reason=None,
    refund_mode=None,
    customer_id=None,
    exchange_cart=None,
    payments=None,
    surplus_outcome="refund",
    override_user_id=None,
    override_reason=None,
    idempotency_key=None,
    on_date=None,
) -> int:
    """Return (or exchange) items from a saved bill. Returns the credit note id.

    items: list of {"sale_line_id", "qty", optional "reason"}.
    outcome:
      'refund'        money goes back (original payment mode unless refund_mode is given).
      'store_credit'  value is added to the customer's store credit (needs a customer).
      'exchange'      exchange_cart (list like a normal bill cart) is sold on a new bill; the
                      credit note pays for it. `payments` covers any extra the customer owes.
                      If the credit is more than the new bill, the surplus follows
                      `surplus_outcome` ('refund' or 'store_credit').
    If the original bill was not fully paid (credit sale), the returned value first
    reduces what the customer still owes; only the rest is refunded.
    Raises ValueError / InsufficientStockError and saves NOTHING when anything is wrong.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"Outcome must be one of: {', '.join(OUTCOMES)}.")
    if surplus_outcome not in ("refund", "store_credit"):
        raise ValueError("Surplus outcome must be 'refund' or 'store_credit'.")
    if outcome != "exchange" and exchange_cart:
        raise ValueError("Replacement items can only be given when the outcome is 'exchange'.")
    on = _on(on_date)
    key = (idempotency_key or "").strip() or None

    with transaction(conn):
        if key:
            existing = conn.execute("SELECT id FROM sale_returns WHERE idempotency_key = ?", (key,)).fetchone()
            if existing:
                return existing["id"]

        _active_user(conn, user_id)
        sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if sale is None:
            raise ValueError("Bill not found.")
        if sale["status"] != "completed":
            raise ValueError("A cancelled bill cannot be returned.")
        if not isinstance(items, (list, tuple)) or not items:
            raise ValueError("Select at least one item to return.")
        if outcome == "exchange" and (not isinstance(exchange_cart, (list, tuple)) or not exchange_cart):
            raise ValueError("Select the replacement items for the exchange.")

        # ---- work out each returned line (quantity limit, value, policy) ----
        seen, lines, problems = set(), [], []
        window = _window_days(conn)
        final_sale = _final_sale_categories(conn)
        for item in items:
            line_id, qty = item.get("sale_line_id"), item.get("qty")
            if line_id in seen:
                raise ValueError("The same bill line is listed twice. Combine the quantities.")
            seen.add(line_id)
            if not isinstance(qty, int) or qty <= 0:
                raise ValueError("Return quantity must be a whole number greater than zero.")
            row = conn.execute(
                "SELECT sl.*, p.category FROM sale_lines sl "
                "JOIN variants v ON v.id = sl.variant_id JOIN products p ON p.id = v.product_id "
                "WHERE sl.id = ? AND sl.sale_id = ?",
                (line_id, sale_id),
            ).fetchone()
            if row is None:
                raise ValueError("A selected item is not on this bill.")
            label = f"{row['product_name']} {row['size']} {row['colour']}"
            line_reason = (item.get("reason") or reason or "").strip()
            if not line_reason:
                raise ValueError(f"Give a reason for returning {label}.")

            done = _returned_qty(conn, row["id"])
            available = row["qty"] - done
            if qty > available:
                raise ValueError(
                    f"{label}: only {available} can still be returned "
                    f"({row['qty']} sold, {done} already returned)."
                    if available > 0
                    else f"{label}: all {row['qty']} sold pieces were already returned."
                )
            if (row["category"] or "").strip().lower() in final_sale:
                problems.append(f"{label} is a final-sale item.")

            taxable = _share(row["taxable_paise"], row["qty"], done + qty) - _share(row["taxable_paise"], row["qty"], done)
            total = _share(row["line_total_paise"], row["qty"], done + qty) - _share(row["line_total_paise"], row["qty"], done)
            lines.append({
                "sale_line": row, "qty": qty, "reason": line_reason, "label": label,
                "taxable_paise": taxable, "tax_paise": total - taxable, "line_total_paise": total,
            })

        if window is not None:
            sale_day = date.fromisoformat(sale["created_at"][:10])
            if date.fromisoformat(on) > sale_day + timedelta(days=window):
                problems.append(f"The return period of {window} days is over.")

        override_id = None
        if problems:
            if override_user_id is None:
                raise ValueError(" ".join(problems) + " Only the owner can override this, with a reason.")
            boss = _active_user(conn, override_user_id, "Override user")
            if boss["role"] != "owner":
                raise ValueError("Only the owner can override the return policy.")
            if not (override_reason or "").strip():
                raise ValueError("Give a reason for overriding the return policy.")
            override_id = boss["id"]

        # ---- credit note amounts ----
        taxable_sum = sum(l["taxable_paise"] for l in lines)
        tax_sum = sum(l["tax_paise"] for l in lines)
        lines_total = sum(l["line_total_paise"] for l in lines)
        # Round the RUNNING total of the bill's returns, not each credit note alone, so
        # returning everything in several steps refunds exactly what the bill came to.
        prior = _prior_lines_total(conn, sale_id)
        total = _round_rupee(prior + lines_total) - _round_rupee(prior)
        round_off = total - lines_total

        due_adjusted = min(total, _due_remaining(conn, sale))
        net = total - due_adjusted

        customer = sale["customer_id"] if sale["customer_id"] is not None else customer_id
        if customer is not None and conn.execute(
            "SELECT 1 FROM customers WHERE id = ? AND is_active = 1", (customer,)
        ).fetchone() is None:
            raise ValueError("Customer not found or not active.")

        # ---- numbering first: the replacement bill refers to this credit note ----
        fin_year = financial_year(on)
        number, credit_note_no = next_number(conn, CREDIT_NOTE_SERIES, fin_year, CREDIT_NOTE_SERIES)

        refund = store_credit = credit_applied = 0
        exchange_sale_id = None
        if outcome == "refund":
            refund = net
        elif outcome == "store_credit":
            store_credit = net
        else:
            exchange_sale_id = save_sale_in_transaction(
                conn, list(exchange_cart), payments or [],
                user_id=user_id,
                customer_id=sale["customer_id"],
                mode=sale["tax_mode"],
                prices_include_tax=bool(sale["prices_include_tax"]),
                intra_state=bool(sale["intra_state"]),
                idempotency_key=f"{key}:exchange" if key else None,
                on_date=on,
                credit_available_paise=net,
                credit_reference=credit_note_no,
            )
            credit_applied = conn.execute(
                "SELECT COALESCE(SUM(amount_paise), 0) FROM payments "
                "WHERE document_type = 'sale' AND document_id = ? AND mode = 'store_credit'",
                (exchange_sale_id,),
            ).fetchone()[0]
            surplus = net - credit_applied
            if surplus_outcome == "refund":
                refund = surplus
            else:
                store_credit = surplus

        if store_credit > 0 and customer is None:
            raise ValueError("Select a customer to give store credit.")
        mode = None
        if refund > 0:
            mode = refund_mode or _default_refund_mode(conn, sale_id)
            if mode not in ACCEPTED_PAYMENT_MODES:
                raise ValueError(
                    f"Refund mode '{mode}' is not available. Use one of: {', '.join(ACCEPTED_PAYMENT_MODES)}."
                )

        # ---- save the credit note ----
        overall_reason = (reason or "").strip() or "; ".join(sorted({l["reason"] for l in lines}))
        cur = conn.execute(
            "INSERT INTO sale_returns(series, number, credit_note_no, fin_year, sale_id, customer_id, user_id, "
            "outcome, reason, taxable_paise, tax_paise, round_off_paise, total_paise, due_adjusted_paise, "
            "refund_paise, store_credit_paise, credit_applied_paise, exchange_sale_id, override_user_id, "
            "override_reason, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                CREDIT_NOTE_SERIES, number, credit_note_no, fin_year, sale_id, customer, user_id,
                outcome, overall_reason, taxable_sum, tax_sum, round_off, total, due_adjusted,
                refund, store_credit, credit_applied, exchange_sale_id, override_id,
                (override_reason or "").strip() if override_id else None, key,
            ),
        )
        return_id = cur.lastrowid

        for l in lines:
            row = l["sale_line"]
            conn.execute(
                "INSERT INTO sale_return_lines(return_id, sale_line_id, variant_id, product_name, size, colour, "
                "qty, taxable_paise, tax_paise, line_total_paise, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    return_id, row["id"], row["variant_id"], row["product_name"], row["size"], row["colour"],
                    l["qty"], l["taxable_paise"], l["tax_paise"], l["line_total_paise"], l["reason"],
                ),
            )
            # Not sellable yet: an authorised user must check the piece first.
            add_movement(
                conn, row["variant_id"], l["qty"], "sale return",
                bucket="returned_pending", doc_type="sale_return", doc_id=return_id, user_id=user_id,
            )

        if refund > 0:
            conn.execute(
                "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, reason, user_id) "
                "VALUES ('sale_return', ?, ?, ?, ?, ?, ?)",
                (return_id, mode, -refund, credit_note_no, overall_reason, user_id),
            )
        if store_credit > 0:
            conn.execute(
                "INSERT INTO store_credit_ledger(customer_id, amount_paise, doc_type, doc_id, note, user_id) "
                "VALUES (?, ?, 'sale_return', ?, ?, ?)",
                (customer, store_credit, return_id, credit_note_no, user_id),
            )

        log_event(
            conn, "sale_return_created", user_id=user_id, entity="sale_return", entity_id=return_id,
            after={
                "credit_note_no": credit_note_no, "invoice_no": sale["invoice_no"], "outcome": outcome,
                "total_paise": total, "refund_paise": refund, "store_credit_paise": store_credit,
                "exchange_sale_id": exchange_sale_id,
                "policy_override_by": override_id, "policy_override_reason": (override_reason or "").strip() or None,
            },
        )
        return return_id