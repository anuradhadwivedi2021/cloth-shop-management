"""Sales counter: save a bill, reprint it, hold and resume bills.

Saving a bill is ONE transaction: invoice number, bill lines, stock reduction,
payments and audit entry are saved together or not at all. A crash, a power
cut or an "insufficient stock" error therefore never leaves a half bill, a gap
in the invoice numbers, or stock that went down without a bill.

Double-click / retry safety: pass the same `idempotency_key` for the same bill
attempt. A second call with that key returns the bill that was already saved
and does not touch stock or payments again.

Printing is separate from saving. Reprinting reads the saved bill, never
changes stock or numbers, and is logged as a duplicate copy.

All money is integer paise.
"""

import json
from datetime import date

from core.audit import log_event
from core.numbering import financial_year, next_number
from core.settings import get_bool
from core.stock import InsufficientStockError, add_movement
from core.tax import calculate_bill, load_tax_rules
from db.connection import transaction

# tax mode -> (series name, invoice prefix)
SALE_SERIES = {
    "regular": ("INV", "INV"),       # tax invoice
    "composition": ("BOS", "BOS"),   # bill of supply
    "unregistered": ("BILL", "BILL"),  # simple bill
}

# store credit, vouchers and loyalty points need their own ledgers (Phase 2);
# until then they are refused so money never "disappears" into an unknown balance.
ACCEPTED_PAYMENT_MODES = ("cash", "upi", "card", "bank_transfer", "cheque", "other")

MAX_HELD_BILLS = 10


def _rs(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def _on(on_date) -> str:
    if on_date is None:
        return date.today().isoformat()
    if isinstance(on_date, date):
        return on_date.isoformat()
    date.fromisoformat(on_date)  # raises ValueError if wrong
    return on_date


def _has_rule_for(rules, hsn, on) -> bool:
    hsn = (hsn or "").strip()
    return bool(hsn) and any(
        hsn.startswith(str(r["hsn_prefix"]))
        and r["valid_from"] <= on
        and (not r.get("valid_to") or on <= r["valid_to"])
        for r in rules
    )


def _label(row) -> str:
    return f"{row['name']} {row['size']} {row['colour']}"


# ---------- paying the bill ----------

def _settle_payments(payments, total, *, credit_ok, customer_id):
    """Work out what is really recorded. Returns (applied, tendered, change, due).

    - Cash handed over above the total is change; only the net cash is recorded.
    - Anything unpaid is a credit sale: allowed only if the shop enabled credit
      sales and a customer is selected.
    """
    applied = []
    for pay in payments or []:
        mode = pay.get("mode")
        amount = pay.get("amount_paise")
        if mode not in ACCEPTED_PAYMENT_MODES:
            raise ValueError(
                f"Payment mode '{mode}' is not available. Use one of: {', '.join(ACCEPTED_PAYMENT_MODES)}."
            )
        if not isinstance(amount, int) or amount <= 0:
            raise ValueError("Each payment must be a positive amount in paise.")
        applied.append({"mode": mode, "amount_paise": amount, "reference": pay.get("reference")})

    tendered = sum(p["amount_paise"] for p in applied)
    change = 0
    due = 0

    if tendered > total:
        excess = tendered - total
        if excess > sum(p["amount_paise"] for p in applied if p["mode"] == "cash"):
            raise ValueError(
                f"Payment is {_rs(excess)} more than the bill total. "
                "Change can only be given from cash."
            )
        change = excess
        for pay in reversed(applied):
            if excess == 0:
                break
            if pay["mode"] == "cash":
                take = min(pay["amount_paise"], excess)
                pay["amount_paise"] -= take
                excess -= take
        applied = [p for p in applied if p["amount_paise"] > 0]
    elif tendered < total:
        due = total - tendered
        if not credit_ok:
            raise ValueError(
                f"Payment is short by {_rs(due)}. "
                "Credit sales are switched off; collect the full amount."
            )
        if customer_id is None:
            raise ValueError("Select a customer to save a bill with an amount due.")
    return applied, tendered, change, due


# ---------- saving a bill ----------
# ---------- saving a bill ----------

def create_sale(conn, cart, payments, **kwargs) -> int:
    """Save a finished bill in ONE transaction. Returns the sale id.

    See `save_sale_in_transaction` for the arguments.
    """
    with transaction(conn):
        return save_sale_in_transaction(conn, cart, payments, **kwargs)


def save_sale_in_transaction(
    conn,
    cart,
    payments,
    *,
    user_id,
    customer_id=None,
    salesperson_id=None,
    mode="regular",
    prices_include_tax=True,
    intra_state=True,
    bill_discount_paise=0,
    idempotency_key=None,
    held_slot=None,
    on_date=None,
    credit_available_paise=0,
    credit_reference=None,
) -> int:
    """Save a finished bill. Returns the sale id.

    Must be called inside `with transaction(conn):` (create_sale does that for
    you). Exchanges call this directly so the credit note and the replacement
    bill are saved together.

    credit_available_paise / credit_reference: value of a credit note that may
    pay for this bill (exchange). Up to the bill total is used; it is recorded as
    a non-cash 'store_credit' payment that points at the credit note number.

    cart: list of {"variant_id", "qty", optional "discount_paise" (item discount),
          optional "rate_percent" (owner-confirmed GST rate)}.
    payments: list of {"mode", "amount_paise", optional "reference"}.
    Prices always come from the catalogue (variant override, else product price).
    held_slot: the held bill this sale finishes; it is removed in the same transaction.
    Raises InsufficientStockError / ValueError and saves NOTHING when anything is wrong.
    """
    on = _on(on_date)
    key = (idempotency_key or "").strip() or None

    if key:
        existing = conn.execute("SELECT id FROM sales WHERE idempotency_key = ?", (key,)).fetchone()
        if existing:
            return existing["id"]

    if conn.execute("SELECT 1 FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone() is None:
        raise ValueError("User not found or not active.")
    if customer_id is not None and conn.execute(
        "SELECT 1 FROM customers WHERE id = ? AND is_active = 1", (customer_id,)
    ).fetchone() is None:
        raise ValueError("Customer not found or not active.")
    if salesperson_id is None:
        salesperson_id = user_id
    if not isinstance(cart, (list, tuple)) or not cart:
        raise ValueError("A bill needs at least one item.")

    rules = load_tax_rules(conn) if mode == "regular" else []
    tax_lines, variants = [], []
    for item in cart:
        qty = item.get("qty")
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("Quantity must be a whole number greater than zero.")
        row = conn.execute(
            "SELECT v.id, v.size, v.colour, v.price_override_paise, v.is_active AS v_active, "
            "p.name, p.hsn, p.gst_rate_percent, p.default_price_paise, p.is_active AS p_active "
            "FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?",
            (item.get("variant_id"),),
        ).fetchone()
        if row is None:
            raise ValueError("A product on this bill was not found.")
        if not (row["v_active"] and row["p_active"]):
            raise ValueError(f"{_label(row)} is inactive and cannot be sold.")
        price = row["price_override_paise"]
        if price is None:
            price = row["default_price_paise"]
        if price <= 0:
            raise ValueError(f"{_label(row)} has no selling price. Set the price first.")

        line = {
            "variant_id": row["id"],
            "hsn": row["hsn"],
            "qty": qty,
            "unit_price_paise": price,
            "discount_paise": item.get("discount_paise", 0),
        }
        if mode == "regular":
            if item.get("rate_percent") is not None:
                line["rate_percent"] = item["rate_percent"]
            elif row["gst_rate_percent"] is not None and not _has_rule_for(rules, row["hsn"], on):
                line["rate_percent"] = row["gst_rate_percent"]  # product rate when no HSN rule applies
        tax_lines.append(line)
        variants.append(row)

    calc = calculate_bill(
        tax_lines,
        on_date=on,
        rules=rules,
        mode=mode,
        prices_include_tax=prices_include_tax,
        bill_discount_paise=bill_discount_paise,
        intra_state=intra_state,
    )
    total = calc["total_paise"]
    credit_used = min(max(int(credit_available_paise or 0), 0), total)
    applied, tendered, change, _due = _settle_payments(
        payments, total - credit_used, credit_ok=get_bool(conn, "credit_sales_enabled"), customer_id=customer_id
    )

    series, prefix = SALE_SERIES[mode]
    fin_year = financial_year(on)
    number, invoice_no = next_number(conn, series, fin_year, prefix)

    cur = conn.execute(
        "INSERT INTO sales(series, number, invoice_no, fin_year, customer_id, salesperson_id, user_id, "
        "tax_mode, subtotal_paise, discount_paise, taxable_paise, tax_paise, round_off_paise, "
        "total_paise, idempotency_key, prices_include_tax, intra_state, calc_method, "
        "tendered_paise, change_paise) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            series, number, invoice_no, fin_year, customer_id, salesperson_id, user_id,
            mode, calc["gross_paise"], calc["item_discount_paise"] + calc["bill_discount_paise"],
            calc["taxable_paise"], calc["tax_paise"], calc["round_off_paise"],
            total, key, int(prices_include_tax), int(intra_state), calc["method"],
            tendered, change,
        ),
    )
    sale_id = cur.lastrowid

    allow_negative = get_bool(conn, "allow_negative_stock")
    for line, row in zip(calc["lines"], variants):
        conn.execute(
            "INSERT INTO sale_lines(sale_id, variant_id, product_name, size, colour, hsn, qty, "
            "unit_price_paise, discount_paise, taxable_paise, tax_rate_percent, tax_paise, "
            "line_total_paise, tax_rule_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sale_id, line["variant_id"], row["name"], row["size"], row["colour"], row["hsn"],
                line["qty"], line["unit_price_paise"],
                line["item_discount_paise"] + line["bill_discount_share_paise"],
                line["taxable_paise"], line["rate_percent"], line["tax_paise"],
                line["line_total_paise"], line["tax_rule_id"],
            ),
        )
        try:
            add_movement(
                conn, line["variant_id"], -line["qty"], "sale",
                doc_type="sale", doc_id=sale_id, user_id=user_id, allow_negative=allow_negative,
            )
        except InsufficientStockError as exc:
            raise InsufficientStockError(f"{_label(row)}: {exc}") from exc

    for pay in applied:
        conn.execute(
            "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, user_id) "
            "VALUES ('sale', ?, ?, ?, ?, ?)",
            (sale_id, pay["mode"], pay["amount_paise"], pay["reference"], user_id),
        )
    if credit_used:
        conn.execute(
            "INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, user_id) "
            "VALUES ('sale', ?, 'store_credit', ?, ?, ?)",
            (sale_id, credit_used, credit_reference, user_id),
        )

    if held_slot is not None:
        conn.execute("DELETE FROM held_bills WHERE slot = ?", (held_slot,))

    log_event(
        conn, "sale_created", user_id=user_id, entity="sale", entity_id=sale_id,
        after={"invoice_no": invoice_no, "total_paise": total, "lines": len(calc["lines"])},
    )
    return sale_id

# ---------- reading a saved bill ----------

def get_sale(conn, sale_id) -> dict:
    """The saved bill with its lines, payments and balance. Raises ValueError if not found."""
    sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if sale is None:
        raise ValueError("Bill not found.")
    sale = dict(sale)

    lines = []
    for row in conn.execute("SELECT * FROM sale_lines WHERE sale_id = ? ORDER BY id", (sale_id,)):
        line = dict(row)
        tax = line["tax_paise"]
        if sale["intra_state"]:
            line["cgst_paise"], line["igst_paise"] = tax // 2, 0
            line["sgst_paise"] = tax - line["cgst_paise"]
        else:
            line["cgst_paise"] = line["sgst_paise"] = 0
            line["igst_paise"] = tax
        lines.append(line)

    payments = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM payments WHERE document_type = 'sale' AND document_id = ? ORDER BY id", (sale_id,)
        )
    ]
    paid = sum(p["amount_paise"] for p in payments)
    due = sale["total_paise"] - paid
    return {
        "sale": sale,
        "lines": lines,
        "payments": payments,
        "paid_paise": paid,
        "due_paise": due,
        "is_fully_paid": due == 0,
    }


def find_sale_by_invoice_no(conn, invoice_no):
    """Sale id for an invoice number (for returns), or None."""
    row = conn.execute(
        "SELECT id FROM sales WHERE upper(invoice_no) = upper(trim(?))", (invoice_no or "",)
    ).fetchone()
    return row["id"] if row else None


def reprint_sale(conn, sale_id, *, user_id) -> dict:
    """Data for printing a duplicate copy. Changes no stock, number or payment; the reprint is logged."""
    with transaction(conn):
        data = get_sale(conn, sale_id)
        copy_number = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'sale_reprint' AND entity = 'sale' AND entity_id = ?",
            (sale_id,),
        ).fetchone()[0] + 1
        log_event(
            conn, "sale_reprint", user_id=user_id, entity="sale", entity_id=sale_id,
            after={"copy_number": copy_number},
        )
    data["copy_label"] = "Duplicate copy"
    data["copy_number"] = copy_number
    return data


# ---------- hold and resume ----------

def hold_bill(conn, cart, *, user_id, customer_id=None, bill_discount_paise=0, note=None) -> int:
    """Park the current cart (max 10). Returns the slot number to resume it by.

    A held bill is only a saved cart: it takes no stock and has no invoice number.
    """
    if not isinstance(cart, (list, tuple)) or not cart:
        raise ValueError("There is nothing to hold.")
    payload = json.dumps({"lines": list(cart), "bill_discount_paise": bill_discount_paise})
    with transaction(conn):
        used = {r[0] for r in conn.execute("SELECT slot FROM held_bills")}
        free = [s for s in range(1, MAX_HELD_BILLS + 1) if s not in used]
        if not free:
            raise ValueError(f"{MAX_HELD_BILLS} bills are already on hold. Resume or discard one first.")
        conn.execute(
            "INSERT INTO held_bills(slot, user_id, customer_id, note, cart_json) VALUES (?,?,?,?,?)",
            (free[0], user_id, customer_id, note, payload),
        )
        return free[0]


def list_held_bills(conn) -> list:
    return [
        {"slot": r["slot"], "customer_id": r["customer_id"], "note": r["note"],
         "created_at": r["created_at"], "items": len(json.loads(r["cart_json"])["lines"])}
        for r in conn.execute("SELECT * FROM held_bills ORDER BY slot")
    ]


def resume_held_bill(conn, slot) -> dict:
    """Read a held bill back. It stays saved until the sale is completed (pass held_slot to create_sale)."""
    row = conn.execute("SELECT * FROM held_bills WHERE slot = ?", (slot,)).fetchone()
    if row is None:
        raise ValueError("Held bill not found.")
    data = json.loads(row["cart_json"])
    return {
        "slot": row["slot"],
        "cart": data["lines"],
        "bill_discount_paise": data["bill_discount_paise"],
        "customer_id": row["customer_id"],
        "note": row["note"],
    }


def discard_held_bill(conn, slot) -> None:
    with transaction(conn):
        if conn.execute("DELETE FROM held_bills WHERE slot = ?", (slot,)).rowcount == 0:
            raise ValueError("Held bill not found.")
