"""Purchases and purchase returns, as the screens use them (SRS 9, 13, PUR-01..06).

The screens call ONLY the functions in this file (and core.supplier_ledger for paying a supplier).
Every write here is ONE transaction that also writes the audit log, and every function checks the
permission first, so the rules hold even if a screen forgets to hide a button.

  * save_draft / receive_purchase / confirm_purchase / cancel_draft   need "receive_purchase".
  * receive_purchase saves the draft and confirms it in the SAME transaction: if confirming fails,
    no half-saved draft is left behind and stock is untouched.
  * Replacing a draft (editing) cancels the old draft and saves the new one in the same transaction.
  * A draft never changes stock. Only confirming adds stock (once; confirming twice is refused).
  * Purchase returns need "return_to_supplier" and use core.purchase_returns (debit note, stock,
    refund and audit entry in one transaction, with an idempotency key against double clicks).

All money is integer paise. Text typed on a screen is turned into numbers by the parse_* helpers,
which raise ValueError with a short message that is safe to show.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from core import products as products_module
from core import purchase_returns as returns_module
from core import purchases as purchases_module
from core import supplier_ledger
from core.audit import log_event
from core.money import format_rupees
from core.permissions import has_permission, require_permission
from core.purchases import PurchaseStateError, calculate_purchase, find_duplicate_purchase
from core.reports_stock import _size_key
from core.stock import InsufficientStockError
from db.connection import transaction

PAYMENT_MODES = supplier_ledger.SUPPLIER_MODES
SETTLEMENTS = returns_module.SETTLEMENTS
MAX_QTY = 100_000
MAX_INVOICE_NO = 40
GENERIC_ERROR = "Something went wrong and nothing was saved."


# ---------------------------------------------------------------- what the screen may show

def safe_message(exc) -> tuple:
    """(text, is_expected) for an error. Expected errors are the short messages written for users
    (ValueError, PermissionDenied, PurchaseStateError, InsufficientStockError). Anything else is a
    fault: the text is generic and the screen must log the real error, never show it."""
    if isinstance(exc, (ValueError, PurchaseStateError, InsufficientStockError)):
        return str(exc) or GENERIC_ERROR, True
    return GENERIC_ERROR, False


# ---------------------------------------------------------------- reading what was typed

def parse_ui_date(text, label="Date") -> str:
    """'04/11/2026' (DD/MM/YYYY, the shop default) or '2026-11-04' -> '2026-11-04'."""
    raw = (text or "").strip()
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", raw)
    try:
        if match:
            return date(int(match[3]), int(match[2]), int(match[1])).isoformat()
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be a valid date, for example 04/11/2026.") from None


def format_ui_date(iso) -> str:
    """'2026-11-04' -> '04/11/2026' (anything else is shown as it is)."""
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(iso or "")


def parse_qty(text, *, label="Quantity") -> int:
    """'12' -> 12. Blank -> 0. Whole numbers only."""
    raw = (text if isinstance(text, str) else str(text or "")).strip()
    if not raw:
        return 0
    if not re.fullmatch(r"\d{1,7}", raw):
        raise ValueError(f"{label} must be a whole number.")
    value = int(raw)
    if value > MAX_QTY:
        raise ValueError(f"{label} is too large.")
    return value


def parse_tax_rate(text) -> float:
    """GST percent typed on the screen, for example '5' or '12.5'. Blank -> 0. The shop decides the rate."""
    raw = (text or "").replace("%", "").strip()
    if not raw:
        return 0.0
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Enter the GST rate as a number, for example 5.") from None
    if not value.is_finite() or value < 0 or value > 100 or value.as_tuple().exponent < -2:
        raise ValueError("The GST rate must be between 0 and 100, with at most 2 decimals.")
    return float(value)


def _iso(value, label="Date") -> str:
    return parse_ui_date(value.isoformat() if isinstance(value, date) else value, label)


# ---------------------------------------------------------------- finding products, the size x colour grid

def find_products(conn, user_id, text, *, limit=30) -> list:
    """Products matching a name, code, brand, size or colour. Cost is shown only to users who may see costs."""
    require_permission(conn, user_id, "receive_purchase", "receive stock")
    show_cost = has_permission(conn, user_id, "view_cost")
    return [
        {
            "id": p["id"], "name": p["name"], "brand": p["brand"] or "", "category": p["category"] or "",
            "unit": p["unit"], "gst_rate_percent": p["gst_rate_percent"],
            "default_cost_paise": p["default_cost_paise"] if show_cost else None,
        }
        for p in products_module.search_products(conn, text, limit=limit)
    ]


def product_matrix(conn, user_id, product_id) -> dict:
    """The size x colour grid of a product: {'product', 'sizes', 'colours', 'cells'}.

    Each cell is {'size', 'colour', 'variant_id', 'sku', 'barcode', 'stock'} (stock = sellable now).
    A size/colour pair with no variant has no cell: the Products screen creates variants.
    """
    require_permission(conn, user_id, "receive_purchase", "receive stock")
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        raise ValueError("Product not found.")
    cells = [
        {
            "size": v["size"], "colour": v["colour"], "variant_id": v["id"], "sku": v["sku"],
            "barcode": v["barcode"], "stock": v["sellable_qty"],
        }
        for v in products_module.list_variants_with_stock(conn, product_id)
        if v["is_active"]
    ]
    if not cells:
        raise ValueError("This product has no sizes or colours yet. Add them in Products first.")
    sizes = sorted({c["size"] for c in cells}, key=_size_key)
    colours = sorted({c["colour"] for c in cells}, key=str.lower)
    show_cost = has_permission(conn, user_id, "view_cost")
    return {
        "product": {
            "id": product["id"], "name": product["name"], "unit": product["unit"],
            "gst_rate_percent": product["gst_rate_percent"],
            "default_cost_paise": product["default_cost_paise"] if show_cost else None,
        },
        "sizes": sizes, "colours": colours, "cells": cells,
    }


def lines_from_matrix(matrix, quantities, *, unit_cost_paise, tax_rate_percent) -> list:
    """Turn {variant_id: quantity} from a grid into purchase lines (zero quantities are skipped)."""
    if not isinstance(unit_cost_paise, int) or isinstance(unit_cost_paise, bool) or unit_cost_paise < 0:
        raise ValueError("Enter the purchase cost per piece.")
    by_id = {c["variant_id"]: c for c in matrix["cells"]}
    lines = []
    for variant_id, qty in quantities.items():
        if variant_id not in by_id:
            raise ValueError("A size or colour in the grid does not belong to this product.")
        if not isinstance(qty, int) or qty < 0:
            raise ValueError("Quantity must be a whole number.")
        if qty == 0:
            continue
        cell = by_id[variant_id]
        lines.append({
            "variant_id": variant_id, "qty": qty, "unit_cost_paise": unit_cost_paise,
            "tax_rate_percent": tax_rate_percent,
            "product": matrix["product"]["name"], "size": cell["size"], "colour": cell["colour"],
        })
    if not lines:
        raise ValueError("Enter a quantity in at least one box of the grid.")
    return sorted(lines, key=lambda l: (_size_key(l["size"]), l["colour"].lower()))


# ---------------------------------------------------------------- reviewing before anything is saved

def _labels(conn, variant_ids) -> dict:
    ids = sorted(set(variant_ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT v.id, p.name, v.size, v.colour, v.barcode FROM variants v JOIN products p ON p.id = v.product_id "
        f"WHERE v.id IN ({marks})", ids,
    ).fetchall()
    return {r["id"]: {"product": r["name"], "size": r["size"], "colour": r["colour"], "barcode": r["barcode"]} for r in rows}


def _clean_lines(lines) -> list:
    """Keep only what the core needs from the lines (screens may add display fields)."""
    if not isinstance(lines, (list, tuple)) or not lines:
        raise ValueError("A purchase needs at least one line.")
    return [
        {
            "variant_id": l.get("variant_id"), "qty": l.get("qty"), "unit_cost_paise": l.get("unit_cost_paise"),
            "tax_rate_percent": l.get("tax_rate_percent", 0),
        }
        for l in lines
    ]


def review_purchase(conn, user_id, lines, *, discount_paise=0, freight_paise=0, paid_paise=0) -> dict:
    """Work out the invoice exactly as it will be saved, without saving anything (for the review step)."""
    require_permission(conn, user_id, "receive_purchase", "record purchases")
    calc = calculate_purchase(_clean_lines(lines), discount_paise=discount_paise, freight_paise=freight_paise)
    if not isinstance(paid_paise, int) or isinstance(paid_paise, bool) or not 0 <= paid_paise <= calc["total_paise"]:
        raise ValueError("Amount paid must be between zero and the invoice total.")
    names = _labels(conn, [l["variant_id"] for l in calc["lines"]])
    shown = []
    for l in calc["lines"]:
        label = names.get(l["variant_id"])
        if label is None:
            raise ValueError("A product variant on this invoice was not found.")
        shown.append({**l, **label})
    return {
        **{k: v for k, v in calc.items() if k != "lines"},
        "lines": shown, "paid_paise": paid_paise, "due_paise": calc["total_paise"] - paid_paise,
        "total_qty": sum(l["qty"] for l in shown),
    }


def duplicate_invoices(conn, user_id, supplier_id, invoice_no, invoice_date) -> list:
    """Earlier purchases from this supplier with the same invoice number in the same financial year.
    A WARNING for the screen to show; the user may still go on."""
    require_permission(conn, user_id, "receive_purchase", "record purchases")
    if not (invoice_no or "").strip():
        return []
    rows = find_duplicate_purchase(conn, supplier_id, invoice_no, _iso(invoice_date, "Invoice date"))
    return [
        {
            "purchase_id": r["id"], "status": r["status"], "voucher_no": r["voucher_no"],
            "invoice_date": r["invoice_date"], "total_paise": r["total_paise"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- saving

def _check_header(conn, supplier_id, invoice_no) -> str:
    invoice_no = (invoice_no or "").strip()
    if not invoice_no:
        raise ValueError("Supplier invoice number is required.")
    if len(invoice_no) > MAX_INVOICE_NO:
        raise ValueError("The invoice number is too long.")
    supplier = conn.execute("SELECT is_active FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if supplier is None:
        raise ValueError("Please choose a supplier.")
    if not supplier["is_active"]:
        raise ValueError("This supplier is disabled. Enable the supplier first.")
    return invoice_no


def _replace_old_draft(conn, user_id, old_id):
    purchase = conn.execute("SELECT status FROM purchases WHERE id = ?", (old_id,)).fetchone()
    if purchase is None:
        raise ValueError("The draft you are editing was not found.")
    purchases_module.cancel_draft_purchase_in_transaction(conn, old_id)
    log_event(conn, "purchase_draft_replaced", user_id=user_id, entity="purchase", entity_id=old_id,
              after={"replaced_by_new_draft": True})


def _create_draft(conn, user_id, supplier_id, invoice_no, invoice_date, lines, *, received_date, discount_paise,
                  freight_paise, paid_paise, notes, replaces_draft_id) -> int:
    require_permission(conn, user_id, "receive_purchase", "record purchases")
    invoice_no = _check_header(conn, supplier_id, invoice_no)
    if replaces_draft_id is not None:
        _replace_old_draft(conn, user_id, replaces_draft_id)
    purchase_id = purchases_module.create_draft_purchase_in_transaction(
        conn, supplier_id, invoice_no, _iso(invoice_date, "Invoice date"), _clean_lines(lines),
        received_date=_iso(received_date, "Received date") if received_date else None,
        discount_paise=discount_paise, freight_paise=freight_paise, paid_paise=paid_paise,
        notes=(notes or "").strip() or None, created_by=user_id,
    )
    row = conn.execute("SELECT total_paise FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    log_event(conn, "purchase_draft_saved", user_id=user_id, entity="purchase", entity_id=purchase_id,
              after={"supplier_id": supplier_id, "invoice_no": invoice_no, "total_paise": row["total_paise"],
                     "lines": len(lines)})
    return purchase_id


def save_draft(conn, user_id, supplier_id, invoice_no, invoice_date, lines, *, received_date=None,
               discount_paise=0, freight_paise=0, paid_paise=0, notes=None, replaces_draft_id=None) -> int:
    """Save a purchase as a DRAFT. Stock does not change. Returns the purchase id.

    replaces_draft_id: the draft being edited. It is cancelled in the same transaction.
    """
    with transaction(conn):
        return _create_draft(
            conn, user_id, supplier_id, invoice_no, invoice_date, lines, received_date=received_date,
            discount_paise=discount_paise, freight_paise=freight_paise, paid_paise=paid_paise, notes=notes,
            replaces_draft_id=replaces_draft_id,
        )


def _confirm(conn, user_id, purchase_id, payment_mode, received_date) -> dict:
    require_permission(conn, user_id, "receive_purchase", "confirm purchases")
    if payment_mode not in PAYMENT_MODES:
        raise ValueError("Choose a valid payment method.")
    received = _iso(received_date, "Received date") if received_date else None
    voucher = purchases_module.confirm_purchase_in_transaction(
        conn, purchase_id, user_id=user_id, payment_mode=payment_mode, received_date=received,
    )
    purchase = conn.execute("SELECT total_paise, paid_paise FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    items = [
        {"variant_id": r["variant_id"], "copies": r["qty"]}
        for r in conn.execute("SELECT variant_id, qty FROM purchase_lines WHERE purchase_id = ? ORDER BY id", (purchase_id,))
    ]
    log_event(conn, "purchase_confirmed", user_id=user_id, entity="purchase", entity_id=purchase_id,
              after={"voucher_no": voucher, "total_paise": purchase["total_paise"],
                     "paid_paise": purchase["paid_paise"], "pieces": sum(i["copies"] for i in items)})
    return {"purchase_id": purchase_id, "voucher_no": voucher, "label_items": items}


def confirm_purchase(conn, user_id, purchase_id, *, payment_mode="cash", received_date=None) -> dict:
    """Confirm a saved draft: stock goes up, the voucher number is taken, any payment is recorded.
    Returns {'purchase_id', 'voucher_no', 'label_items'} (label_items is ready for print_labels)."""
    with transaction(conn):
        return _confirm(conn, user_id, purchase_id, payment_mode, received_date)


def receive_purchase(conn, user_id, supplier_id, invoice_no, invoice_date, lines, *, received_date=None,
                     discount_paise=0, freight_paise=0, paid_paise=0, notes=None, payment_mode="cash",
                     replaces_draft_id=None) -> dict:
    """Save the invoice AND confirm it in one transaction (the usual 'receive stock' action)."""
    with transaction(conn):
        purchase_id = _create_draft(
            conn, user_id, supplier_id, invoice_no, invoice_date, lines, received_date=received_date,
            discount_paise=discount_paise, freight_paise=freight_paise, paid_paise=paid_paise, notes=notes,
            replaces_draft_id=replaces_draft_id,
        )
        return _confirm(conn, user_id, purchase_id, payment_mode, received_date or invoice_date)


def cancel_draft(conn, user_id, purchase_id, reason=None) -> None:
    """Cancel a DRAFT. The record stays (status 'cancelled'). A confirmed purchase needs a purchase return."""
    with transaction(conn):
        require_permission(conn, user_id, "receive_purchase", "cancel purchases")
        purchases_module.cancel_draft_purchase_in_transaction(conn, purchase_id)
        log_event(conn, "purchase_draft_cancelled", user_id=user_id, entity="purchase", entity_id=purchase_id,
                  after={"reason": (reason or "").strip() or None})


# ---------------------------------------------------------------- reading saved purchases

def list_purchases(conn, user_id, *, status=None, supplier_id=None, text=None, limit=200) -> list:
    """Purchases, newest first. For a confirmed purchase 'paid' and 'outstanding' are the real figures
    (payments and debit notes); for a draft they are what was typed."""
    require_permission(conn, user_id, "receive_purchase", "see purchases")
    if status not in (None, "draft", "confirmed", "cancelled"):
        raise ValueError("Unknown purchase status.")
    sql = (
        "SELECT pu.id AS id, pu.supplier_id, su.name AS supplier_name, pu.invoice_no, pu.invoice_date, "
        "pu.voucher_no, pu.status, pu.total_paise, pu.paid_paise, pu.due_paise, "
        "(SELECT COALESCE(SUM(qty), 0) FROM purchase_lines WHERE purchase_id = pu.id) AS qty "
        "FROM purchases pu JOIN suppliers su ON su.id = pu.supplier_id WHERE 1 = 1"
    )
    args = []
    if status:
        sql += " AND pu.status = ?"
        args.append(status)
    if supplier_id is not None:
        sql += " AND pu.supplier_id = ?"
        args.append(supplier_id)
    needle = (text or "").strip()
    if needle:
        sql += " AND (pu.invoice_no LIKE ? OR pu.voucher_no LIKE ? OR su.name LIKE ?)"
        args += [f"%{needle}%"] * 3
    out = []
    for r in conn.execute(sql + " ORDER BY pu.id DESC LIMIT ?", (*args, limit)).fetchall():
        row = dict(r)
        if r["status"] == "confirmed":
            fig = supplier_ledger._bill_figures(conn, r)
            row["paid_paise"], row["outstanding_paise"] = fig["paid_paise"], fig["outstanding_paise"]
        else:
            row["outstanding_paise"] = r["due_paise"]
        out.append(row)
    return out


def purchase_detail(conn, user_id, purchase_id) -> dict:
    """One purchase with its lines (product, size, colour) for showing or copying into the form."""
    require_permission(conn, user_id, "receive_purchase", "see purchases")
    purchase, lines = purchases_module.get_purchase(conn, purchase_id)
    supplier = conn.execute("SELECT name FROM suppliers WHERE id = ?", (purchase["supplier_id"],)).fetchone()
    names = _labels(conn, [l["variant_id"] for l in lines])
    return {
        "purchase": dict(purchase), "supplier_name": supplier["name"] if supplier else "",
        "lines": [
            {**dict(l), **names.get(l["variant_id"], {"product": "", "size": "", "colour": "", "barcode": None})}
            for l in lines
        ],
    }


# ---------------------------------------------------------------- paying a supplier

def pay_supplier_bill(conn, user_id, purchase_id, amount_paise, mode, *, reference=None, notes=None,
                      payment_date=None, idempotency_key=None) -> int:
    """Pay (part of) one confirmed bill. Returns the supplier payment id. Needs "manage_cash".
    The same idempotency_key twice saves only one payment."""
    require_permission(conn, user_id, "manage_cash", "pay suppliers")
    purchase = conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    if purchase is None:
        raise ValueError("Purchase not found.")
    if purchase["status"] != "confirmed":
        raise ValueError("Only a confirmed purchase can be paid.")
    owed = supplier_ledger._bill_figures(conn, purchase)["outstanding_paise"]
    if isinstance(amount_paise, int) and not isinstance(amount_paise, bool) and amount_paise > owed:
        raise ValueError(f"This bill only has {format_rupees(owed)} left to pay.")
    return supplier_ledger.pay_supplier(
        conn, user_id, purchase["supplier_id"], amount_paise, mode, purchase_id=purchase_id,
        reference=reference, notes=notes, payment_date=payment_date, idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------- purchase returns

def find_returnable_purchases(conn, user_id, *, supplier_id=None, text=None, limit=100) -> list:
    """Confirmed purchases that still have pieces which can go back to the supplier."""
    require_permission(conn, user_id, "return_to_supplier", "return goods to a supplier")
    sql = (
        "SELECT * FROM (SELECT pu.id AS purchase_id, pu.supplier_id, su.name AS supplier_name, pu.invoice_no, "
        "pu.invoice_date, pu.voucher_no, pu.total_paise, "
        "(SELECT COALESCE(SUM(qty), 0) FROM purchase_lines WHERE purchase_id = pu.id) AS bought, "
        "(SELECT COALESCE(SUM(rl.qty), 0) FROM purchase_return_lines rl JOIN purchase_lines pl "
        " ON pl.id = rl.purchase_line_id WHERE pl.purchase_id = pu.id) AS returned "
        "FROM purchases pu JOIN suppliers su ON su.id = pu.supplier_id WHERE pu.status = 'confirmed'"
    )
    args = []
    if supplier_id is not None:
        sql += " AND pu.supplier_id = ?"
        args.append(supplier_id)
    needle = (text or "").strip()
    if needle:
        sql += " AND (pu.invoice_no LIKE ? OR pu.voucher_no LIKE ? OR su.name LIKE ?)"
        args += [f"%{needle}%"] * 3
    sql += ") WHERE bought > returned ORDER BY invoice_date DESC, purchase_id DESC LIMIT ?"
    return [dict(r, returnable=r["bought"] - r["returned"]) for r in conn.execute(sql, (*args, limit)).fetchall()]


def returnable_lines(conn, user_id, purchase_id) -> list:
    """The lines of a purchase with how many pieces can go back now (never more than is in stock)."""
    require_permission(conn, user_id, "return_to_supplier", "return goods to a supplier")
    purchase = conn.execute("SELECT status FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    if purchase is None:
        raise ValueError("Purchase not found.")
    if purchase["status"] != "confirmed":
        raise ValueError("Only a confirmed purchase can be returned.")
    lines = []
    for l in returns_module.returnable_purchase_lines(conn, purchase_id):
        lines.append({**l, "max_qty": max(min(l["qty_returnable"], l["sellable_now"]), 0)})
    return lines


def return_to_supplier(conn, user_id, purchase_id, quantities, reason, *, settlement="refund", refund_mode=None,
                       idempotency_key=None) -> dict:
    """Send pieces back to the supplier. quantities: {purchase_line_id: pieces} (zeros are ignored).

    Saves the debit note, reduces stock, records any refund and the audit entry in ONE transaction.
    Returns {'return_id', 'debit_note_no', 'total_paise', 'due_reduced_paise', 'refund_paise',
    'supplier_credit_paise'}.
    """
    items = [{"purchase_line_id": line_id, "qty": qty} for line_id, qty in (quantities or {}).items() if qty]
    if not items:
        raise ValueError("Enter how many pieces to return.")
    return_id = returns_module.process_purchase_return(
        conn, purchase_id, items, user_id=user_id, reason=reason, settlement=settlement,
        refund_mode=refund_mode, idempotency_key=idempotency_key,
    )
    note = returns_module.get_purchase_return(conn, return_id)["return"]
    return {
        "return_id": return_id, "debit_note_no": note["debit_note_no"], "total_paise": note["total_paise"],
        "due_reduced_paise": note["due_reduced_paise"], "refund_paise": note["refund_paise"],
        "supplier_credit_paise": note["supplier_credit_paise"],
    }


def purchase_return_history(conn, user_id, purchase_id) -> list:
    """Debit notes already made against a purchase."""
    require_permission(conn, user_id, "return_to_supplier", "return goods to a supplier")
    return [
        {
            "return_id": r["id"], "debit_note_no": r["debit_note_no"], "date": (r["created_at"] or "")[:10],
            "total_paise": r["total_paise"], "refund_paise": r["refund_paise"],
            "supplier_credit_paise": r["supplier_credit_paise"], "reason": r["reason"],
        }
        for r in returns_module.list_purchase_returns(conn, purchase_id)
    ]