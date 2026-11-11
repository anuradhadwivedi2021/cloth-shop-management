"""BIL-12: estimates (price quotes) for customers, and turning one into a real bill.

  create_estimate(conn, user_id, cart, ...)    quote the items now. Takes NO stock, makes NO sale.
  get_estimate / list_estimates                read them (price change since the quote is shown)
  cancel_estimate(conn, user_id, estimate_id)  close an open estimate that will not be used
  convert_estimate(conn, user_id, id, payments, ...)   make the real bill from an open estimate

Rules:
  * An estimate has its own number series (EST) and is never a tax invoice. It does not touch stock, payments,
    the invoice numbers or any report of sales.
  * The quote shows the price at the time. A real bill always uses TODAY's catalogue price, so converting can
    give a different total: the result says so ('quoted_total_paise' and 'billed_total_paise').
  * Converting is safe to repeat: the bill carries the key "estimate-<id>", so a retry after a crash finds the
    bill that was already made and just closes the estimate. One estimate can never become two bills.
  * An estimate past its date cannot be converted; make a new one. Closed estimates are final.
"""

from datetime import date, timedelta

from core import bill_preview as bp
from core.audit import log_event
from core.guarded import sell
from core.numbering import financial_year, next_number
from core.permissions import require_permission
from core.shop_settings import billing_problems, get_shop_profile
from db.connection import transaction

MAX_LINES = 100
MAX_VALID_DAYS = 90
DEFAULT_VALID_DAYS = 7
MAX_NOTE = 500


def _today(today):
    return today or date.today()


def _mode(conn) -> str:
    scheme = get_shop_profile(conn)["tax_scheme"]
    return scheme if scheme in ("regular", "composition", "unregistered") else "regular"


def _clean_cart(cart) -> list:
    if not isinstance(cart, (list, tuple)) or not cart:
        raise ValueError("Add at least one item to the estimate.")
    if len(cart) > MAX_LINES:
        raise ValueError(f"An estimate can have at most {MAX_LINES} lines.")
    clean = []
    for line in cart:
        qty = line.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError("Each line needs a quantity of at least 1.")
        discount = line.get("discount_paise", 0)
        if isinstance(discount, bool) or not isinstance(discount, int) or discount < 0:
            raise ValueError("A discount cannot be negative.")
        clean.append({"variant_id": line.get("variant_id"), "qty": qty, "discount_paise": discount})
    return clean


def create_estimate(conn, user_id, cart, *, customer_id=None, customer_name=None, note=None,
                    valid_days=DEFAULT_VALID_DAYS, today=None) -> int:
    """Save a quote. Returns the estimate id. Needs the permission to sell."""
    require_permission(conn, user_id, "sell", "make an estimate")
    cart = _clean_cart(cart)
    if isinstance(valid_days, bool) or not isinstance(valid_days, int) or not 1 <= valid_days <= MAX_VALID_DAYS:
        raise ValueError(f"An estimate can be valid for 1 to {MAX_VALID_DAYS} days.")
    name = " ".join(str(customer_name or "").split())[:100] or None
    note = (str(note).strip()[:MAX_NOTE] if note else None) or None
    on = _today(today)
    mode = _mode(conn)
    problems = billing_problems(conn, mode)
    if problems:
        raise ValueError(" ".join(problems))
    if customer_id is not None and not conn.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone():
        raise ValueError("The customer was not found.")
    preview = bp.preview_bill(conn, cart, mode=mode, on_date=on.isoformat())      # prices, GST, totals; stock is not needed
    if preview["total_paise"] <= 0:
        raise ValueError("The estimate total must be more than zero.")
    with transaction(conn):
        number, estimate_no = next_number(conn, "EST", financial_year(on), "EST")
        cur = conn.execute(
            "INSERT INTO estimates(number, estimate_no, fin_year, customer_id, customer_name, note, valid_until, "
            "total_paise, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (number, estimate_no, financial_year(on), customer_id, name, note,
             (on + timedelta(days=valid_days)).isoformat(), preview["total_paise"], user_id))
        estimate_id = cur.lastrowid
        for line in preview["lines"]:
            conn.execute(
                "INSERT INTO estimate_lines(estimate_id, variant_id, label, qty, unit_price_paise, discount_paise, "
                "line_total_paise) VALUES (?,?,?,?,?,?,?)",
                (estimate_id, line["variant_id"], line["label"], line["qty"], line["unit_price_paise"],
                 line["discount_paise"], line["line_total_paise"]))
        log_event(conn, "estimate_created", user_id=user_id, entity="estimate", entity_id=estimate_id,
                  after={"estimate_no": estimate_no, "total_paise": preview["total_paise"], "lines": len(preview["lines"])})
    return estimate_id


def _shape(conn, row, on) -> dict:
    estimate = dict(row)
    estimate["expired"] = estimate["status"] == "open" and estimate["valid_until"] < on.isoformat()
    estimate["lines"] = [dict(r) for r in conn.execute(
        "SELECT * FROM estimate_lines WHERE estimate_id = ? ORDER BY id", (estimate["id"],))]
    return estimate


def get_estimate(conn, user_id, estimate_id, *, today=None) -> dict:
    """The estimate with its lines. For an open one, 'current_total_paise' is what the same items cost today."""
    require_permission(conn, user_id, "sell", "see estimates")
    row = conn.execute("SELECT * FROM estimates WHERE id = ?", (estimate_id,)).fetchone()
    if row is None:
        raise ValueError("Estimate not found.")
    on = _today(today)
    estimate = _shape(conn, row, on)
    estimate["current_total_paise"] = None
    if estimate["status"] == "open":
        try:
            now = bp.preview_bill(conn, [{"variant_id": l["variant_id"], "qty": l["qty"], "discount_paise": l["discount_paise"]}
                                         for l in estimate["lines"]], mode=_mode(conn), on_date=on.isoformat())
            estimate["current_total_paise"] = now["total_paise"]
        except ValueError:
            pass                                      # an item became inactive: the screen shows no current total
    estimate["price_changed"] = (estimate["current_total_paise"] is not None
                                 and estimate["current_total_paise"] != estimate["total_paise"])
    return estimate


def list_estimates(conn, user_id, *, status=None, limit=200, today=None) -> list:
    """Newest first. status: 'open', 'converted', 'cancelled', 'expired' or None for all."""
    require_permission(conn, user_id, "sell", "see estimates")
    on = _today(today)
    where, args = [], []
    if status in ("open", "converted", "cancelled"):
        where.append("status = ?")
        args.append(status)
    if status == "open":
        where.append("valid_until >= ?")
        args.append(on.isoformat())
    if status == "expired":
        where.append("status = 'open' AND valid_until < ?")
        args.append(on.isoformat())
    rows = conn.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM estimate_lines l WHERE l.estimate_id = e.id) AS line_count FROM estimates e"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY e.id DESC LIMIT ?",
        (*args, max(1, min(int(limit), 1000)))).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["expired"] = item["status"] == "open" and item["valid_until"] < on.isoformat()
        result.append(item)
    return result


def cancel_estimate(conn, user_id, estimate_id) -> None:
    require_permission(conn, user_id, "sell", "cancel an estimate")
    with transaction(conn):
        row = conn.execute("SELECT status, estimate_no FROM estimates WHERE id = ?", (estimate_id,)).fetchone()
        if row is None:
            raise ValueError("Estimate not found.")
        if row["status"] != "open":
            raise ValueError("Only an open estimate can be cancelled.")
        conn.execute("UPDATE estimates SET status = 'cancelled', closed_at = strftime('%Y-%m-%d %H:%M:%S','now','localtime') "
                     "WHERE id = ?", (estimate_id,))
        log_event(conn, "estimate_cancelled", user_id=user_id, entity="estimate", entity_id=estimate_id,
                  after={"estimate_no": row["estimate_no"]})


def convert_estimate(conn, user_id, estimate_id, payments, *, approver_pin=None, today=None, **sale_kwargs) -> dict:
    """Make the real bill from an open estimate. Returns {'sale_id', 'invoice_no', 'quoted_total_paise', 'billed_total_paise'}.

    `payments` are those of the new bill (as for any bill). Stock, permission and discount-limit checks are the
    normal ones of selling; if any fails nothing is saved and the estimate stays open.
    """
    require_permission(conn, user_id, "sell", "make a sale")
    row = conn.execute("SELECT * FROM estimates WHERE id = ?", (estimate_id,)).fetchone()
    if row is None:
        raise ValueError("Estimate not found.")
    on = _today(today)
    key = f"estimate-{estimate_id}"
    already = conn.execute("SELECT id FROM sales WHERE idempotency_key = ?", (key,)).fetchone()
    if row["status"] == "converted" or already:
        # a repeat (or a retry after a crash between the bill and the closing): never make a second bill
        sale_id = row["sale_id"] or already["id"]
    elif row["status"] == "cancelled":
        raise ValueError("This estimate was cancelled.")
    elif row["valid_until"] < on.isoformat():
        raise ValueError("This estimate has expired. Make a new estimate.")
    else:
        lines = [{"variant_id": r["variant_id"], "qty": r["qty"], "discount_paise": r["discount_paise"]}
                 for r in conn.execute("SELECT * FROM estimate_lines WHERE estimate_id = ? ORDER BY id", (estimate_id,))]
        sale_kwargs.setdefault("mode", _mode(conn))
        sale_kwargs.setdefault("customer_id", row["customer_id"])
        sale_id = sell(conn, lines, payments, user_id=user_id, approver_pin=approver_pin, idempotency_key=key,
                       on_date=on.isoformat(), **sale_kwargs)
    with transaction(conn):
        current = conn.execute("SELECT status FROM estimates WHERE id = ?", (estimate_id,)).fetchone()["status"]
        if current == "open":
            conn.execute("UPDATE estimates SET status = 'converted', sale_id = ?, "
                         "closed_at = strftime('%Y-%m-%d %H:%M:%S','now','localtime') WHERE id = ?", (sale_id, estimate_id))
            log_event(conn, "estimate_converted", user_id=user_id, entity="estimate", entity_id=estimate_id,
                      after={"estimate_no": row["estimate_no"], "sale_id": sale_id})
    sale = conn.execute("SELECT total_paise, invoice_no FROM sales WHERE id = ?", (sale_id,)).fetchone()
    return {"sale_id": sale_id, "invoice_no": sale["invoice_no"], "quoted_total_paise": row["total_paise"],
            "billed_total_paise": sale["total_paise"]}
