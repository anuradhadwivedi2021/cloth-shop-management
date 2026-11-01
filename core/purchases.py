"""Suppliers and purchases.

A purchase starts as a DRAFT and does not touch stock. Only confirming it adds
stock (through stock movements), allocates the voucher number and records any
payment, all in one transaction. A confirmed purchase can never be confirmed twice.

All money is integer paise. Tax is rounded per line (half up); the bill total is
rounded to the nearest rupee with the round-off kept as its own figure.
The tax and rounding method must be confirmed with the owner's accountant.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from core.numbering import financial_year, next_number
from core.stock import add_movement
from db.connection import transaction


class PurchaseStateError(Exception):
    """Raised when an action is not allowed for the purchase's current status."""


# ---------- suppliers ----------

def create_supplier(
    conn, name, *, contact_person=None, phone=None, email=None, address=None, gstin=None, state=None
):
    """Create a supplier. Returns the supplier id."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Supplier name is required.")
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO suppliers(name, contact_person, phone, email, address, gstin, state) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, contact_person, phone, email, address, gstin, state),
        )
        return cur.lastrowid


# ---------- calculation (pure, no database) ----------

def _tax_paise(qty: int, unit_cost_paise: int, rate_percent) -> int:
    amount = Decimal(qty * unit_cost_paise) * Decimal(str(rate_percent)) / Decimal(100)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_purchase(lines, *, discount_paise: int = 0, freight_paise: int = 0):
    """Work out line tax and invoice totals.

    lines: list of dicts with variant_id, qty, unit_cost_paise, tax_rate_percent (optional).
    Returns a dict with the computed lines and subtotal, tax, discount, freight,
    round_off and total (all paise).
    """
    if not lines:
        raise ValueError("A purchase needs at least one line.")
    if discount_paise < 0 or freight_paise < 0:
        raise ValueError("Discount and freight cannot be negative.")

    computed = []
    subtotal = 0
    tax_total = 0
    for line in lines:
        qty = line.get("qty")
        cost = line.get("unit_cost_paise")
        rate = line.get("tax_rate_percent", 0)
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("Quantity must be a whole number greater than zero.")
        if not isinstance(cost, int) or cost < 0:
            raise ValueError("Unit cost must be a non-negative amount in paise.")
        if Decimal(str(rate)) < 0:
            raise ValueError("Tax rate cannot be negative.")
        tax = _tax_paise(qty, cost, rate)
        computed.append(
            {
                "variant_id": line.get("variant_id"),
                "qty": qty,
                "unit_cost_paise": cost,
                "tax_rate_percent": float(rate),
                "tax_paise": tax,
                "line_total_paise": qty * cost + tax,
            }
        )
        subtotal += qty * cost
        tax_total += tax

    before_round = subtotal + tax_total + freight_paise - discount_paise
    if before_round < 0:
        raise ValueError("Discount cannot be larger than the invoice amount.")
    rounded = ((before_round + 50) // 100) * 100
    return {
        "lines": computed,
        "subtotal_paise": subtotal,
        "tax_paise": tax_total,
        "discount_paise": discount_paise,
        "freight_paise": freight_paise,
        "round_off_paise": rounded - before_round,
        "total_paise": rounded,
    }


# ---------- purchases ----------

def _iso_date(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def find_duplicate_purchase(conn, supplier_id, invoice_no, invoice_date):
    """Earlier purchases with the same supplier invoice number in the same financial year.

    This is a WARNING for the screen to show, not a block. Cancelled drafts are ignored.
    """
    fin_year = financial_year(_iso_date(invoice_date))
    return conn.execute(
        "SELECT * FROM purchases WHERE supplier_id = ? AND fin_year = ? "
        "AND lower(trim(invoice_no)) = lower(trim(?)) AND status != 'cancelled' ORDER BY id",
        (supplier_id, fin_year, invoice_no or ""),
    ).fetchall()


def create_draft_purchase(
    conn,
    supplier_id,
    invoice_no,
    invoice_date,
    lines,
    *,
    received_date=None,
    discount_paise: int = 0,
    freight_paise: int = 0,
    paid_paise: int = 0,
    notes=None,
    created_by=None,
):
    """Save a purchase as a DRAFT. Stock does not change. Returns the purchase id."""
    invoice_no = (invoice_no or "").strip()
    if not invoice_no:
        raise ValueError("Supplier invoice number is required.")
    invoice_date = _iso_date(invoice_date)
    fin_year = financial_year(invoice_date)  # also validates the date format
    calc = calculate_purchase(lines, discount_paise=discount_paise, freight_paise=freight_paise)
    if not 0 <= paid_paise <= calc["total_paise"]:
        raise ValueError("Amount paid must be between zero and the invoice total.")

    with transaction(conn):
        if conn.execute("SELECT 1 FROM suppliers WHERE id = ?", (supplier_id,)).fetchone() is None:
            raise ValueError("Supplier not found.")
        for line in calc["lines"]:
            if conn.execute("SELECT 1 FROM variants WHERE id = ?", (line["variant_id"],)).fetchone() is None:
                raise ValueError("A product variant on this invoice was not found.")

        cur = conn.execute(
            "INSERT INTO purchases(supplier_id, invoice_no, invoice_date, received_date, fin_year, status, "
            "subtotal_paise, discount_paise, tax_paise, freight_paise, round_off_paise, total_paise, "
            "paid_paise, due_paise, notes, created_by) "
            "VALUES (?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?)",
            (
                supplier_id, invoice_no, invoice_date, received_date, fin_year,
                calc["subtotal_paise"], calc["discount_paise"], calc["tax_paise"],
                calc["freight_paise"], calc["round_off_paise"], calc["total_paise"],
                paid_paise, calc["total_paise"] - paid_paise, notes, created_by,
            ),
        )
        purchase_id = cur.lastrowid
        for line in calc["lines"]:
            conn.execute(
                "INSERT INTO purchase_lines(purchase_id, variant_id, qty, unit_cost_paise, "
                "tax_rate_percent, tax_paise, line_total_paise) VALUES (?,?,?,?,?,?,?)",
                (
                    purchase_id, line["variant_id"], line["qty"], line["unit_cost_paise"],
                    line["tax_rate_percent"], line["tax_paise"], line["line_total_paise"],
                ),
            )
        return purchase_id


def confirm_purchase(conn, purchase_id, *, user_id=None, payment_mode="cash", received_date=None):
    """Confirm a draft: add stock, allocate the voucher number, record payment. Returns the voucher number.

    Everything happens in ONE transaction. If anything fails, nothing is saved.
    Confirming a purchase that is not a draft is refused, so double-clicks cannot add stock twice.
    """
    with transaction(conn):
        purchase = conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
        if purchase is None:
            raise ValueError("Purchase not found.")
        if purchase["status"] != "draft":
            raise PurchaseStateError(
                f"This purchase is already {purchase['status']} and cannot be confirmed again."
            )
        lines = conn.execute(
            "SELECT * FROM purchase_lines WHERE purchase_id = ? ORDER BY id", (purchase_id,)
        ).fetchall()
        if not lines:
            raise ValueError("This purchase has no lines.")

        _, voucher_no = next_number(conn, "PUR", purchase["fin_year"], "PUR")

        for line in lines:
            add_movement(
                conn,
                line["variant_id"],
                line["qty"],
                "purchase received",
                doc_type="purchase",
                doc_id=purchase_id,
                user_id=user_id,
            )

        if purchase["paid_paise"] > 0:
            conn.execute(
                "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, user_id) "
                "VALUES ('purchase', ?, ?, ?, ?, ?)",
                (purchase_id, payment_mode, purchase["paid_paise"], purchase["invoice_no"], user_id),
            )

        conn.execute(
            "UPDATE purchases SET status = 'confirmed', voucher_no = ?, "
            "received_date = COALESCE(?, received_date, ?), confirmed_at = datetime('now','localtime') "
            "WHERE id = ?",
            (voucher_no, received_date, date.today().isoformat(), purchase_id),
        )
        return voucher_no


def cancel_draft_purchase(conn, purchase_id):
    """Cancel a DRAFT purchase. Confirmed purchases need a purchase return instead."""
    with transaction(conn):
        purchase = conn.execute("SELECT status FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
        if purchase is None:
            raise ValueError("Purchase not found.")
        if purchase["status"] != "draft":
            raise PurchaseStateError(
                f"Only a draft can be cancelled; this purchase is {purchase['status']}."
            )
        conn.execute("UPDATE purchases SET status = 'cancelled' WHERE id = ?", (purchase_id,))


def get_purchase(conn, purchase_id):
    """Returns (purchase_row, [line_rows])."""
    purchase = conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    if purchase is None:
        raise ValueError("Purchase not found.")
    lines = conn.execute(
        "SELECT * FROM purchase_lines WHERE purchase_id = ? ORDER BY id", (purchase_id,)
    ).fetchall()
    return purchase, lines