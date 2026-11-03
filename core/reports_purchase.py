"""Purchase reports (SRS 16.3, PUR-08, PUR-09 and the Purchase and Tax groups in section 8).

Rules every report here follows:
* Only CONFIRMED purchases count. Drafts and cancelled drafts never appear in any figure.
* A purchase belongs to its supplier-invoice date; a purchase return (debit note) to the date it
  was issued. Each document is counted once.
* Amounts show what the shop paid its suppliers, so every report needs "view_reports" AND
  "view_cost" (a cashier or a plain report viewer without cost access cannot see them).
* Cost and tax are kept apart: cost is before GST, GST is shown as its own figure.
* Money still owed uses the same figures as the supplier ledger, so the two never disagree.
"""

from core.permissions import require_permission
from core.report_common import check_range, require_view
from core.supplier_ledger import _bill_figures

GROUPS = ("supplier", "product", "variant", "category", "brand", "day", "month")
NET_COST_FORMULA = "Net cost = purchase cost - cost of goods returned to suppliers (both before GST)"
GST_PURCHASE_NOTE = (
    "Input GST by rate, net of debit notes. A purchase-level discount is not spread across lines, and the "
    "split into CGST / SGST / IGST depends on the supplier's state. Check this with your accountant."
)

# group -> (key for purchase lines, key for returned lines)
_KEYS = {
    "supplier": ("su.name", "su.name"),
    "product": ("p.name", "p.name"),
    "variant": ("p.name || ' / ' || v.size || ' / ' || v.colour",) * 2,
    "category": ("COALESCE(p.category, '(none)')",) * 2,
    "brand": ("COALESCE(p.brand, '(none)')",) * 2,
    "day": ("pu.invoice_date", "substr(r.created_at, 1, 10)"),
    "month": ("substr(pu.invoice_date, 1, 7)", "substr(r.created_at, 1, 7)"),
}


def _require_cost(conn, user_id):
    require_view(conn, user_id)
    require_permission(conn, user_id, "view_cost", "see purchase costs")


_BOUGHT = (
    "FROM purchase_lines pl JOIN purchases pu ON pu.id = pl.purchase_id JOIN suppliers su ON su.id = pu.supplier_id "
    "JOIN variants v ON v.id = pl.variant_id JOIN products p ON p.id = v.product_id "
    "WHERE pu.status = 'confirmed' AND pu.invoice_date BETWEEN ? AND ?"
)
_RETURNED = (
    "FROM purchase_return_lines rl JOIN purchase_returns r ON r.id = rl.return_id "
    "JOIN purchases pu ON pu.id = r.purchase_id JOIN suppliers su ON su.id = r.supplier_id "
    "JOIN variants v ON v.id = rl.variant_id JOIN products p ON p.id = v.product_id "
    "WHERE substr(r.created_at, 1, 10) BETWEEN ? AND ?"
)


# ---------- purchase register ----------

def purchase_register(conn, date_from, date_to, *, user_id, supplier_id=None) -> dict:
    """Every confirmed purchase in the period with its payments, returns and what is still owed."""
    _require_cost(conn, user_id)
    start, end = check_range(date_from, date_to)
    sql = (
        "SELECT pu.id AS purchase_id, pu.voucher_no, pu.invoice_no, pu.invoice_date, pu.received_date, "
        "pu.supplier_id, su.name AS supplier_name, su.gstin AS supplier_gstin, pu.subtotal_paise, pu.discount_paise, "
        "pu.tax_paise, pu.freight_paise, pu.round_off_paise, pu.total_paise, "
        "(SELECT COALESCE(SUM(qty), 0) FROM purchase_lines WHERE purchase_id = pu.id) AS qty, "
        "(SELECT COALESCE(SUM(total_paise), 0) FROM purchase_returns WHERE purchase_id = pu.id) AS returns_paise, "
        "pu.id AS id FROM purchases pu JOIN suppliers su ON su.id = pu.supplier_id "
        "WHERE pu.status = 'confirmed' AND pu.invoice_date BETWEEN ? AND ?"
    )
    args = [start, end]
    if supplier_id is not None:
        sql += " AND pu.supplier_id = ?"
        args.append(supplier_id)
    rows = []
    for r in conn.execute(sql + " ORDER BY pu.invoice_date, pu.id", args).fetchall():
        figures = _bill_figures(conn, r)
        row = {k: r[k] for k in r.keys() if k != "id"}
        row.update(
            paid_paise=figures["paid_paise"], returns_credit_paise=figures["returns_credit_paise"],
            outstanding_paise=figures["outstanding_paise"],
        )
        rows.append(row)
    keys = ("qty", "subtotal_paise", "discount_paise", "tax_paise", "freight_paise", "round_off_paise",
            "total_paise", "returns_paise", "paid_paise", "outstanding_paise")
    return {
        "date_from": start, "date_to": end, "rows": rows, "purchase_count": len(rows),
        "totals": {k: sum(r[k] for r in rows) for k in keys},
    }


# ---------- grouped purchase totals ----------

def purchases_by(conn, date_from, date_to, group_by, *, user_id) -> dict:
    """Purchases less returns, grouped by supplier, product, variant, category, brand, day or month."""
    _require_cost(conn, user_id)
    start, end = check_range(date_from, date_to)
    if group_by not in GROUPS:
        raise ValueError(f"Group by must be one of: {', '.join(GROUPS)}.")
    bought_key, returned_key = _KEYS[group_by]

    table = {}

    def entry(key):
        return table.setdefault(key, dict.fromkeys(
            ("qty", "cost_paise", "tax_paise", "returned_qty", "returned_cost_paise", "returned_tax_paise"), 0))

    for r in conn.execute(
        f"SELECT {bought_key} AS key, SUM(pl.qty) AS qty, SUM(pl.qty * pl.unit_cost_paise) AS cost, "
        f"SUM(pl.tax_paise) AS tax {_BOUGHT} GROUP BY key", (start, end),
    ):
        e = entry(r["key"])
        e["qty"], e["cost_paise"], e["tax_paise"] = r["qty"], r["cost"], r["tax"]
    for r in conn.execute(
        f"SELECT {returned_key} AS key, SUM(rl.qty) AS qty, SUM(rl.qty * rl.unit_cost_paise) AS cost, "
        f"SUM(rl.tax_paise) AS tax {_RETURNED} GROUP BY key", (start, end),
    ):
        e = entry(r["key"])
        e["returned_qty"], e["returned_cost_paise"], e["returned_tax_paise"] = r["qty"], r["cost"], r["tax"]

    rows = []
    for key, e in table.items():
        rows.append({
            "key": key, **e, "net_qty": e["qty"] - e["returned_qty"],
            "net_cost_paise": e["cost_paise"] - e["returned_cost_paise"],
            "net_tax_paise": e["tax_paise"] - e["returned_tax_paise"],
        })
    if group_by in ("day", "month"):
        rows.sort(key=lambda r: r["key"])
    else:
        rows.sort(key=lambda r: (-r["net_cost_paise"], r["key"].lower()))
    total_keys = ("qty", "cost_paise", "tax_paise", "returned_qty", "returned_cost_paise", "returned_tax_paise",
                  "net_qty", "net_cost_paise", "net_tax_paise")
    return {
        "date_from": start, "date_to": end, "group_by": group_by, "rows": rows,
        "totals": {k: sum(r[k] for r in rows) for k in total_keys}, "formula": NET_COST_FORMULA,
    }


# ---------- purchase returns (debit notes) ----------

def purchase_return_register(conn, date_from, date_to, *, user_id, supplier_id=None) -> dict:
    """Every debit note issued in the period."""
    _require_cost(conn, user_id)
    start, end = check_range(date_from, date_to)
    sql = (
        "SELECT r.id AS return_id, r.debit_note_no, substr(r.created_at, 1, 10) AS date, r.purchase_id, "
        "pu.invoice_no AS purchase_invoice_no, r.supplier_id, su.name AS supplier_name, r.reason, "
        "(SELECT COALESCE(SUM(qty), 0) FROM purchase_return_lines WHERE return_id = r.id) AS qty, "
        "r.subtotal_paise, r.tax_paise, r.total_paise, r.due_reduced_paise, r.refund_paise, r.supplier_credit_paise "
        "FROM purchase_returns r JOIN purchases pu ON pu.id = r.purchase_id JOIN suppliers su ON su.id = r.supplier_id "
        "WHERE substr(r.created_at, 1, 10) BETWEEN ? AND ?"
    )
    args = [start, end]
    if supplier_id is not None:
        sql += " AND r.supplier_id = ?"
        args.append(supplier_id)
    rows = [{k: r[k] for k in r.keys()} for r in conn.execute(sql + " ORDER BY r.id", args)]
    keys = ("qty", "subtotal_paise", "tax_paise", "total_paise", "due_reduced_paise", "refund_paise",
            "supplier_credit_paise")
    return {
        "date_from": start, "date_to": end, "rows": rows, "return_count": len(rows),
        "totals": {k: sum(r[k] for r in rows) for k in keys},
    }


# ---------- price history ----------

def price_history(conn, variant_id, *, user_id, supplier_id=None) -> dict:
    """What a variant cost on every confirmed purchase, newest first, with the change from the one before."""
    _require_cost(conn, user_id)
    variant = conn.execute(
        "SELECT v.id AS variant_id, p.name AS product, v.size, v.colour FROM variants v "
        "JOIN products p ON p.id = v.product_id WHERE v.id = ?", (variant_id,),
    ).fetchone()
    if variant is None:
        raise ValueError("Variant not found.")
    sql = (
        "SELECT pu.id AS purchase_id, pu.invoice_date, pu.invoice_no, pu.supplier_id, su.name AS supplier_name, "
        "pl.qty, pl.unit_cost_paise, pl.tax_rate_percent FROM purchase_lines pl "
        "JOIN purchases pu ON pu.id = pl.purchase_id JOIN suppliers su ON su.id = pu.supplier_id "
        "WHERE pl.variant_id = ? AND pu.status = 'confirmed'"
    )
    args = [variant_id]
    if supplier_id is not None:
        sql += " AND pu.supplier_id = ?"
        args.append(supplier_id)
    oldest_first = [{k: r[k] for k in r.keys()} for r in conn.execute(sql + " ORDER BY pu.invoice_date, pu.id", args)]
    previous = None
    for row in oldest_first:
        row["change_paise"] = None if previous is None else row["unit_cost_paise"] - previous
        previous = row["unit_cost_paise"]
    costs = [r["unit_cost_paise"] for r in oldest_first]
    return {
        "variant": {k: variant[k] for k in variant.keys()},
        "rows": list(reversed(oldest_first)),
        "lowest_paise": min(costs) if costs else None,
        "highest_paise": max(costs) if costs else None,
        "latest_paise": costs[-1] if costs else None,
    }


# ---------- GST on purchases (input tax) ----------

def _gst_rows(conn, start, end, bought_sql, returned_sql, key_name):
    table = {}

    def entry(key, extra):
        return table.setdefault(key, {
            **extra, "purchases_taxable_paise": 0, "purchases_tax_paise": 0, "invoice_count": 0,
            "returns_taxable_paise": 0, "returns_tax_paise": 0,
        })

    for r in conn.execute(bought_sql, (start, end)):
        e = entry(r[key_name], {k: r[k] for k in r.keys() if k not in ("taxable", "tax", "invoices")})
        e["purchases_taxable_paise"], e["purchases_tax_paise"], e["invoice_count"] = r["taxable"], r["tax"], r["invoices"]
    for r in conn.execute(returned_sql, (start, end)):
        e = entry(r[key_name], {k: r[k] for k in r.keys() if k not in ("taxable", "tax")})
        e["returns_taxable_paise"], e["returns_tax_paise"] = r["taxable"], r["tax"]
    rows = []
    for e in table.values():
        e["net_taxable_paise"] = e["purchases_taxable_paise"] - e["returns_taxable_paise"]
        e["net_tax_paise"] = e["purchases_tax_paise"] - e["returns_tax_paise"]
        rows.append(e)
    return rows


def gst_purchase_summary(conn, date_from, date_to, *, user_id) -> dict:
    """Input GST on confirmed purchases, by rate and by supplier, net of debit notes."""
    _require_cost(conn, user_id)
    start, end = check_range(date_from, date_to)

    by_rate = _gst_rows(
        conn, start, end,
        "SELECT pl.tax_rate_percent AS rate_percent, SUM(pl.qty * pl.unit_cost_paise) AS taxable, "
        "SUM(pl.tax_paise) AS tax, COUNT(DISTINCT pu.id) AS invoices FROM purchase_lines pl "
        "JOIN purchases pu ON pu.id = pl.purchase_id WHERE pu.status = 'confirmed' "
        "AND pu.invoice_date BETWEEN ? AND ? GROUP BY pl.tax_rate_percent",
        "SELECT pl.tax_rate_percent AS rate_percent, SUM(rl.qty * rl.unit_cost_paise) AS taxable, "
        "SUM(rl.tax_paise) AS tax FROM purchase_return_lines rl JOIN purchase_returns r ON r.id = rl.return_id "
        "JOIN purchase_lines pl ON pl.id = rl.purchase_line_id "
        "WHERE substr(r.created_at, 1, 10) BETWEEN ? AND ? GROUP BY pl.tax_rate_percent",
        "rate_percent",
    )
    by_supplier = _gst_rows(
        conn, start, end,
        "SELECT su.id AS supplier_id, su.name AS supplier_name, su.gstin AS supplier_gstin, "
        "SUM(pl.qty * pl.unit_cost_paise) AS taxable, SUM(pl.tax_paise) AS tax, COUNT(DISTINCT pu.id) AS invoices "
        "FROM purchase_lines pl JOIN purchases pu ON pu.id = pl.purchase_id JOIN suppliers su ON su.id = pu.supplier_id "
        "WHERE pu.status = 'confirmed' AND pu.invoice_date BETWEEN ? AND ? GROUP BY su.id",
        "SELECT su.id AS supplier_id, su.name AS supplier_name, su.gstin AS supplier_gstin, "
        "SUM(rl.qty * rl.unit_cost_paise) AS taxable, SUM(rl.tax_paise) AS tax FROM purchase_return_lines rl "
        "JOIN purchase_returns r ON r.id = rl.return_id JOIN suppliers su ON su.id = r.supplier_id "
        "WHERE substr(r.created_at, 1, 10) BETWEEN ? AND ? GROUP BY su.id",
        "supplier_id",
    )
    by_rate.sort(key=lambda r: r["rate_percent"])
    by_supplier.sort(key=lambda r: r["supplier_name"].lower())
    keys = ("purchases_taxable_paise", "purchases_tax_paise", "returns_taxable_paise", "returns_tax_paise",
            "net_taxable_paise", "net_tax_paise")
    return {
        "date_from": start, "date_to": end, "by_rate": by_rate, "by_supplier": by_supplier,
        "totals": {k: sum(r[k] for r in by_rate) for k in keys}, "note": GST_PURCHASE_NOTE,
    }
