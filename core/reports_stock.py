"""Stock reports (SRS 7.3, 16.2, STK-06, STK-10 and the Stock group in section 8).

Rules every report here follows:
* Stock figures come ONLY from stock movements (the `variant_stock` view), never from a stored
  number. Each report therefore agrees with the movement ledger by construction.
* Only the SELLABLE bucket is "stock you can bill". Damaged, returned-pending-check, on-trial and
  reserved quantities are shown separately and never mixed in.
* Reading needs the "view_reports" permission. Anything showing purchase cost or stock value
  also needs "view_cost".
* Suggestions (reorder list) never place an order.
"""

from datetime import date

from core.permissions import has_permission, require_permission
from core.report_common import iso_date, require_view

BUCKETS = ("sellable", "damaged", "returned_pending", "on_trial", "reserved")
VALUATION_METHODS = {
    "weighted_average": "Weighted average cost (purchases less purchase returns)",
    "last_purchase_cost": "Last purchase cost",
}
REORDER_NOTE = "Suggestions only. Nothing is ordered automatically."
_SIZE_ORDER = ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "3XL", "4XL", "5XL"]


# ---------- helpers ----------

def _size_key(size):
    text = (size or "").strip().upper()
    if text in _SIZE_ORDER:
        return (0, _SIZE_ORDER.index(text), "")
    try:
        return (1, float(text), "")
    except ValueError:
        return (2, 0, text)


def _status(sellable, reorder):
    if sellable <= 0:
        return "out"
    if reorder and sellable < reorder:
        return "low"
    return "ok"


def _require_cost(conn, user_id):
    require_view(conn, user_id)
    require_permission(conn, user_id, "view_cost", "see purchase cost and stock value")


# ---------- stock by variant ----------

def current_stock(
    conn, *, user_id, product_id=None, category=None, brand=None, search=None,
    status=None, include_inactive=False,
) -> list:
    """Current stock of every variant, one row each, with every bucket shown separately.

    status: None (all), 'ok', 'low' (below the reorder level) or 'out' (nothing sellable).
    The reorder level is the variant's own level if set, otherwise the product's.
    """
    require_view(conn, user_id)
    return _stock_rows(
        conn, product_id=product_id, category=category, brand=brand, search=search,
        status=status, include_inactive=include_inactive,
    )


def stock_overview(
    conn, *, user_id, product_id=None, category=None, brand=None, search=None,
    status=None, include_inactive=False,
) -> list:
    """Same rows as current_stock, for anyone who may see products and stock ('view_products').

    Shows quantities only: no cost and no stock value, so it is safe for cashiers and stock staff.
    """
    require_permission(conn, user_id, "view_products", "see products and stock")
    return _stock_rows(
        conn, product_id=product_id, category=category, brand=brand, search=search,
        status=status, include_inactive=include_inactive,
    )


def _stock_rows(conn, *, product_id, category, brand, search, status, include_inactive) -> list:
    if status not in (None, "ok", "low", "out"):
        raise ValueError("Status must be ok, low or out.")
    sql = (
        "SELECT p.id AS product_id, p.name AS product, p.category, p.brand, v.id AS variant_id, v.size, v.colour, "
        "v.sku, v.barcode, v.rack_location, s.sellable_qty, s.damaged_qty, s.returned_pending_qty, "
        "s.on_trial_qty, s.reserved_qty, COALESCE(v.reorder_level, p.reorder_level) AS reorder_level "
        "FROM variants v JOIN products p ON p.id = v.product_id JOIN variant_stock s ON s.variant_id = v.id "
        "WHERE 1 = 1"
    )
    args = []
    if not include_inactive:
        sql += " AND v.is_active = 1 AND p.is_active = 1"
    if product_id is not None:
        sql += " AND p.id = ?"
        args.append(product_id)
    if category:
        sql += " AND lower(p.category) = lower(?)"
        args.append(category.strip())
    if brand:
        sql += " AND lower(p.brand) = lower(?)"
        args.append(brand.strip())
    if search and search.strip():
        like = f"%{search.strip()}%"
        sql += (" AND (p.name LIKE ? OR p.brand LIKE ? OR p.category LIKE ? OR v.sku LIKE ? OR v.barcode LIKE ? "
                "OR v.size LIKE ? OR v.colour LIKE ? OR v.rack_location LIKE ?)")
        args += [like] * 8
    rows = []
    for r in conn.execute(sql, args).fetchall():
        row = {k: r[k] for k in r.keys()}
        row["status"] = _status(row["sellable_qty"], row["reorder_level"])
        if status is None or row["status"] == status:
            rows.append(row)
    rows.sort(key=lambda x: (x["product"].lower(), x["product_id"], (x["colour"] or "").lower(), _size_key(x["size"])))
    return rows


def stock_grid(conn, product_id, *, user_id) -> dict:
    """Size x colour table of sellable stock for one product. None = that combination does not exist."""
    require_view(conn, user_id)
    product = conn.execute("SELECT id, name FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        raise ValueError("Product not found.")
    rows = conn.execute(
        "SELECT v.size, v.colour, s.sellable_qty FROM variants v JOIN variant_stock s ON s.variant_id = v.id "
        "WHERE v.product_id = ? AND v.is_active = 1",
        (product_id,),
    ).fetchall()
    sizes = sorted({r["size"] for r in rows}, key=_size_key)
    colours = sorted({r["colour"] for r in rows}, key=lambda c: c.lower())
    cell = {(r["size"], r["colour"]): r["sellable_qty"] for r in rows}
    grid_rows = []
    for colour in colours:
        quantities = [cell.get((size, colour)) for size in sizes]
        grid_rows.append({"colour": colour, "quantities": quantities, "total": sum(q for q in quantities if q)})
    size_totals = [sum(cell.get((size, c), 0) for c in colours) for size in sizes]
    return {
        "product_id": product["id"], "product": product["name"], "sizes": sizes, "colours": colours,
        "rows": grid_rows, "size_totals": size_totals, "total": sum(size_totals),
    }


# ---------- low stock and reorder ----------

def low_stock(conn, *, user_id) -> list:
    """Variants whose sellable stock is below their reorder level (a level of 0 means 'not set')."""
    rows = [r for r in current_stock(conn, user_id=user_id) if r["reorder_level"] and r["status"] != "ok"]
    for r in rows:
        r["shortfall"] = r["reorder_level"] - r["sellable_qty"]
    return rows


def _last_purchase(conn, variant_id):
    return conn.execute(
        "SELECT p.supplier_id, su.name AS supplier_name, pl.unit_cost_paise FROM purchase_lines pl "
        "JOIN purchases p ON p.id = pl.purchase_id JOIN suppliers su ON su.id = p.supplier_id "
        "WHERE pl.variant_id = ? AND p.status = 'confirmed' ORDER BY p.confirmed_at DESC, p.id DESC LIMIT 1",
        (variant_id,),
    ).fetchone()


def reorder_list(conn, *, user_id, target_multiple=2) -> dict:
    """What to order, grouped by the supplier each item was last bought from.

    Suggested quantity = (reorder level x target_multiple) - current stock, at least 1.
    The last purchase cost is shown only to users who have the "view_cost" permission.
    """
    if not isinstance(target_multiple, int) or target_multiple < 1:
        raise ValueError("Target multiple must be a whole number of at least 1.")
    can_see_cost = has_permission(conn, user_id, "view_cost")
    groups = {}
    for item in low_stock(conn, user_id=user_id):
        last = _last_purchase(conn, item["variant_id"])
        key = last["supplier_id"] if last else None
        group = groups.setdefault(
            key, {"supplier_id": key, "supplier_name": last["supplier_name"] if last else "No supplier recorded", "items": []}
        )
        group["items"].append({
            "variant_id": item["variant_id"], "product": item["product"], "size": item["size"],
            "colour": item["colour"], "sku": item["sku"], "current_qty": item["sellable_qty"],
            "reorder_level": item["reorder_level"],
            "suggested_qty": max(item["reorder_level"] * target_multiple - item["sellable_qty"], 1),
            "last_cost_paise": last["unit_cost_paise"] if (last and can_see_cost) else None,
        })
    ordered = sorted(groups.values(), key=lambda g: (g["supplier_id"] is None, g["supplier_name"].lower()))
    return {"groups": ordered, "item_count": sum(len(g["items"]) for g in ordered), "note": REORDER_NOTE}


# ---------- movement ledger and flow ----------

def movement_ledger(conn, variant_id, *, user_id, date_from=None, date_to=None) -> dict:
    """Every stock movement of one variant, with opening and closing quantity of each bucket."""
    require_view(conn, user_id)
    variant = conn.execute(
        "SELECT v.id AS variant_id, p.name AS product, v.size, v.colour, v.sku FROM variants v "
        "JOIN products p ON p.id = v.product_id WHERE v.id = ?", (variant_id,),
    ).fetchone()
    if variant is None:
        raise ValueError("Variant not found.")
    start = iso_date(date_from, "From date") if date_from else None
    end = iso_date(date_to, "To date") if date_to else None
    if start and end and start > end:
        raise ValueError("The From date cannot be after the To date.")

    opening = dict.fromkeys(BUCKETS, 0)
    if start:
        for r in conn.execute(
            "SELECT bucket, SUM(qty_change) AS q FROM stock_movements WHERE variant_id = ? "
            "AND substr(created_at, 1, 10) < ? GROUP BY bucket", (variant_id, start),
        ):
            opening[r["bucket"]] = r["q"]

    sql = (
        "SELECT m.id, m.created_at, m.bucket, m.qty_change, m.balance_after, m.reason, m.doc_type, m.doc_id, "
        "COALESCE(u.full_name, u.username) AS user_name FROM stock_movements m "
        "LEFT JOIN users u ON u.id = m.user_id WHERE m.variant_id = ?"
    )
    args = [variant_id]
    if start:
        sql += " AND substr(m.created_at, 1, 10) >= ?"
        args.append(start)
    if end:
        sql += " AND substr(m.created_at, 1, 10) <= ?"
        args.append(end)
    rows = [{k: r[k] for k in r.keys()} for r in conn.execute(sql + " ORDER BY m.id", args)]
    closing = dict(opening)
    for r in rows:
        closing[r["bucket"]] += r["qty_change"]
    return {
        "variant": {k: variant[k] for k in variant.keys()}, "date_from": start, "date_to": end,
        "opening": opening, "rows": rows, "closing": closing,
    }


_IN_RANGE = "substr(m.created_at, 1, 10) BETWEEN :start AND :end"
_OTHER = "(m.doc_type IS NULL OR m.doc_type NOT IN ('purchase', 'purchase_return', 'sale', 'stock_adjustment', 'stock_count'))"


def _doc(name):
    return f"m.doc_type = '{name}'"


def _range_sum(condition, sign=""):
    return f"COALESCE({sign}SUM(CASE WHEN {_IN_RANGE} AND {condition} THEN m.qty_change END), 0)"


def stock_flow(conn, date_from, date_to, *, user_id, product_id=None) -> dict:
    """Sellable stock received and sold in a period, per variant (SRS 16.2).

    opening + received - purchase_returned - sold + adjustments + count_differences + other = closing
    """
    require_view(conn, user_id)
    start, end = iso_date(date_from, "From date"), iso_date(date_to, "To date")
    if start > end:
        raise ValueError("The From date cannot be after the To date.")

    sql = (
        "SELECT v.id AS variant_id, p.name AS product, v.size, v.colour, "
        "COALESCE(SUM(CASE WHEN substr(m.created_at, 1, 10) < :start THEN m.qty_change END), 0) AS opening, "
        f"{_range_sum(_doc('purchase'))} AS received, "
        f"{_range_sum(_doc('purchase_return'), '-')} AS purchase_returned, "
        f"{_range_sum(_doc('sale'), '-')} AS sold, "
        f"{_range_sum(_doc('stock_adjustment'))} AS adjustments, "
        f"{_range_sum(_doc('stock_count'))} AS count_differences, "
        f"{_range_sum(_OTHER)} AS other, "
        "COALESCE(SUM(m.qty_change), 0) AS closing "
        "FROM stock_movements m JOIN variants v ON v.id = m.variant_id JOIN products p ON p.id = v.product_id "
        "WHERE m.bucket = 'sellable' AND substr(m.created_at, 1, 10) <= :end"
    )
    params = {"start": start, "end": end}
    if product_id is not None:
        sql += " AND p.id = :product_id"
        params["product_id"] = product_id
    sql += (
        " GROUP BY v.id HAVING opening != 0 OR received != 0 OR purchase_returned != 0 OR sold != 0"
        " OR adjustments != 0 OR count_differences != 0 OR other != 0"
    )
    rows = [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params)]
    rows.sort(key=lambda x: (x["product"].lower(), (x["colour"] or "").lower(), _size_key(x["size"])))
    totals = {k: sum(r[k] for r in rows) for k in (
        "opening", "received", "purchase_returned", "sold", "adjustments", "count_differences", "other", "closing")}
    return {
        "date_from": start, "date_to": end, "rows": rows, "totals": totals,
        "formula": "opening + received - purchase returned - sold + adjustments + count differences + other = closing",
    }


# ---------- items that did not sell (slow-moving stock) ----------

UNSOLD_NOTE = (
    "Only stock you can sell today is listed. 'Pieces sold' counts the pieces on bills in the period; returns are "
    "not subtracted. 'Days since last sale' counts up to the last day of the period. Never sold = no sale on record."
)


def unsold_stock(conn, date_from, date_to, *, user_id, max_sold=0, category=None, brand=None) -> dict:
    """Stock on the shelf that sold little or nothing in a period (PDF 1 section 16.5: slow-moving items and items
    with no sales). max_sold=0 lists items that did not sell at all; 2 lists items that sold 2 pieces or fewer.

    Oldest sale first, items that never sold at the top. Stock value (at the last purchase cost) is added only for
    users who may see costs.
    """
    require_view(conn, user_id)
    start, end = iso_date(date_from, "From date"), iso_date(date_to, "To date")
    if start > end:
        raise ValueError("The From date cannot be after the To date.")
    if isinstance(max_sold, bool) or not isinstance(max_sold, int) or max_sold < 0:
        raise ValueError("The number of pieces must be zero or more.")
    show_cost = has_permission(conn, user_id, "view_cost")

    stock = _stock_rows(conn, product_id=None, category=category, brand=brand, search=None, status=None,
                        include_inactive=False)
    sold = {r["variant_id"]: r["qty"] for r in conn.execute(
        "SELECT m.variant_id, -SUM(m.qty_change) AS qty FROM stock_movements m WHERE m.bucket = 'sellable' "
        "AND m.doc_type = 'sale' AND substr(m.created_at, 1, 10) BETWEEN ? AND ? GROUP BY m.variant_id", (start, end))}
    last = {r["variant_id"]: r["day"] for r in conn.execute(
        "SELECT m.variant_id, MAX(substr(m.created_at, 1, 10)) AS day FROM stock_movements m WHERE m.bucket = 'sellable' "
        "AND m.doc_type = 'sale' AND substr(m.created_at, 1, 10) <= ? GROUP BY m.variant_id", (end,))}
    end_day = date.fromisoformat(end)

    rows = []
    for r in stock:
        if r["sellable_qty"] <= 0:
            continue
        pieces = sold.get(r["variant_id"], 0)
        if pieces > max_sold:
            continue
        last_day = last.get(r["variant_id"])
        row = {
            "product": r["product"], "category": r["category"], "brand": r["brand"], "size": r["size"],
            "colour": r["colour"], "sku": r["sku"], "rack_location": r["rack_location"],
            "in_stock": r["sellable_qty"], "pieces_sold": pieces, "last_sold": last_day,
            "days_since_last_sale": (end_day - date.fromisoformat(last_day)).days if last_day else None,
        }
        if show_cost:
            bought = _last_purchase(conn, r["variant_id"])
            row["stock_value_paise"] = r["sellable_qty"] * bought["unit_cost_paise"] if bought else None
        rows.append(row)
    rows.sort(key=lambda x: (x["days_since_last_sale"] is not None, -(x["days_since_last_sale"] or 0),
                             x["product"].lower(), (x["colour"] or "").lower(), _size_key(x["size"])))
    result = {
        "date_from": start, "date_to": end, "max_sold": max_sold, "variants": len(rows),
        "units_in_stock": sum(r["in_stock"] for r in rows), "rows": rows, "note": UNSOLD_NOTE,
    }
    if show_cost:
        result["stock_value_paise"] = sum(r["stock_value_paise"] or 0 for r in rows)
    return result


# ---------- unsellable stock ----------

def damaged_stock(conn, *, user_id) -> dict:
    """Stock that cannot be billed: damaged, and returned items waiting for a resale check."""
    rows = [
        r for r in current_stock(conn, user_id=user_id, include_inactive=True)
        if r["damaged_qty"] or r["returned_pending_qty"]
    ]
    return {
        "rows": rows,
        "damaged_units": sum(r["damaged_qty"] for r in rows),
        "returned_pending_units": sum(r["returned_pending_qty"] for r in rows),
    }


# ---------- valuation ----------

def _cost_tables(conn):
    purchased = {
        r["variant_id"]: (r["qty"], r["cost"])
        for r in conn.execute(
            "SELECT pl.variant_id, SUM(pl.qty) AS qty, SUM(pl.qty * pl.unit_cost_paise) AS cost FROM purchase_lines pl "
            "JOIN purchases p ON p.id = pl.purchase_id WHERE p.status = 'confirmed' GROUP BY pl.variant_id"
        )
    }
    returned = {
        r["variant_id"]: (r["qty"], r["cost"])
        for r in conn.execute(
            "SELECT variant_id, SUM(qty) AS qty, SUM(qty * unit_cost_paise) AS cost FROM purchase_return_lines "
            "GROUP BY variant_id"
        )
    }
    last = {
        r["variant_id"]: r["unit_cost_paise"]
        for r in conn.execute(
            "SELECT pl.variant_id, pl.unit_cost_paise FROM purchase_lines pl JOIN purchases p ON p.id = pl.purchase_id "
            "WHERE p.status = 'confirmed' AND pl.id = (SELECT pl2.id FROM purchase_lines pl2 "
            "JOIN purchases p2 ON p2.id = pl2.purchase_id WHERE pl2.variant_id = pl.variant_id AND p2.status = 'confirmed' "
            "ORDER BY p2.confirmed_at DESC, pl2.id DESC LIMIT 1)"
        )
    }
    return purchased, returned, last


def stock_valuation(conn, *, user_id, method="weighted_average", category=None, brand=None) -> dict:
    """Value of SELLABLE stock at cost, with the method named on the report (needs "view_cost").

    weighted_average: (purchased cost - returned cost) / (purchased qty - returned qty), per variant.
    last_purchase_cost: the unit cost on the variant's latest confirmed purchase.
    Cost is before GST. A variant with no purchase uses the product's default cost; with neither,
    it is valued at zero and counted in `variants_without_cost`.
    """
    _require_cost(conn, user_id)
    if method not in VALUATION_METHODS:
        raise ValueError(f"Method must be one of: {', '.join(VALUATION_METHODS)}.")
    purchased, returned, last = _cost_tables(conn)
    defaults = {r["id"]: r["default_cost_paise"] for r in conn.execute("SELECT id, default_cost_paise FROM products")}
    variant_product = {r["id"]: r["product_id"] for r in conn.execute("SELECT id, product_id FROM variants")}

    rows = []
    for item in current_stock(conn, user_id=user_id, category=category, brand=brand):
        qty = item["sellable_qty"]
        if qty <= 0:
            continue
        vid = item["variant_id"]
        total_cost = None  # exact value in paise when the method gives an average
        unit = None
        source = "purchases"
        if method == "weighted_average":
            bought_qty, bought_cost = purchased.get(vid, (0, 0))
            back_qty, back_cost = returned.get(vid, (0, 0))
            net_qty, net_cost = bought_qty - back_qty, bought_cost - back_cost
            if net_qty > 0:
                unit = (2 * net_cost + net_qty) // (2 * net_qty)
                total_cost = (2 * qty * net_cost + net_qty) // (2 * net_qty)
        elif vid in last:
            unit = last[vid]
        if unit is None:
            default = defaults.get(variant_product.get(vid), 0)
            unit, source = (default, "product_default") if default > 0 else (0, "missing")
        rows.append({
            "variant_id": vid, "product": item["product"], "category": item["category"], "size": item["size"],
            "colour": item["colour"], "sellable_qty": qty, "unit_cost_paise": unit,
            "value_paise": total_cost if total_cost is not None else qty * unit, "cost_source": source,
        })
    return {
        "method": method, "method_label": VALUATION_METHODS[method], "rows": rows,
        "total_qty": sum(r["sellable_qty"] for r in rows),
        "total_value_paise": sum(r["value_paise"] for r in rows),
        "variants_without_cost": sum(1 for r in rows if r["cost_source"] == "missing"),
        "note": "Sellable stock only, at cost before GST. Damaged and unsellable stock is excluded.",
    }