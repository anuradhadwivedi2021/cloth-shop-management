"""Read-only helpers for the billing counter screen.

Nothing here changes data. The counter uses these to SHOW what a bill will look like and which
size/colour is in stock; the bill itself is saved only by core.guarded.sell.

preview_bill() follows the same steps as core.sales.save_sale_in_transaction (catalogue price,
product GST rate when no HSN rule applies, same calculate_bill call), so the total shown on screen
is the total that gets saved. All money is integer paise.
"""

from datetime import date

from core.products import find_variant_by_code
from core.sales import _has_rule_for
from core.tax import calculate_bill, load_tax_rules

_SELECT = (
    "SELECT v.id AS variant_id, v.size, v.colour, v.barcode, v.sku, v.price_override_paise, "
    "v.is_active AS v_active, p.id AS product_id, p.name, p.hsn, p.gst_rate_percent, "
    "p.default_price_paise, p.is_active AS p_active, "
    # a sum for just this variant: joining the variant_stock view works out every variant first (slow on a big shop)
    "COALESCE((SELECT SUM(m.qty_change) FROM stock_movements m "
    "WHERE m.variant_id = v.id AND m.bucket = 'sellable'), 0) AS sellable "
    "FROM variants v JOIN products p ON p.id = v.product_id "
)


def _shape(row) -> dict:
    price = row["price_override_paise"]
    if price is None:
        price = row["default_price_paise"]
    return {
        "variant_id": row["variant_id"],
        "product_id": row["product_id"],
        "name": row["name"],
        "size": row["size"],
        "colour": row["colour"],
        "barcode": row["barcode"],
        "sku": row["sku"],
        "hsn": row["hsn"],
        "gst_rate_percent": row["gst_rate_percent"],
        "price_paise": price,
        "sellable": row["sellable"],
        "is_active": bool(row["v_active"] and row["p_active"]),
        "label": f"{row['name']} {row['size']} {row['colour']}",
    }


def variant_info(conn, variant_id):
    """One variant with its selling price and sellable stock, or None if it does not exist."""
    row = conn.execute(_SELECT + "WHERE v.id = ?", (variant_id,)).fetchone()
    return _shape(row) if row else None


def find_by_code(conn, code):
    """Variant for a scanned or typed barcode / supplier barcode / SKU, or None."""
    found = find_variant_by_code(conn, code)
    return variant_info(conn, found["id"]) if found else None


def variants_of_product(conn, product_id) -> list:
    """Active variants of an active product, with price and sellable stock (for the size x colour picker)."""
    rows = conn.execute(
        _SELECT + "WHERE v.product_id = ? AND v.is_active = 1 AND p.is_active = 1 ORDER BY v.id",
        (product_id,),
    ).fetchall()
    return [_shape(r) for r in rows]


def alternatives(conn, variant_id, limit=5) -> list:
    """Other in-stock variants of the same product (shown when the wanted size/colour is out of stock)."""
    info = variant_info(conn, variant_id)
    if info is None:
        return []
    others = [v for v in variants_of_product(conn, info["product_id"])
              if v["variant_id"] != variant_id and v["sellable"] > 0]
    return others[:limit]


def preview_bill(conn, cart, *, mode="regular", intra_state=True, bill_discount_paise=0,
                 prices_include_tax=True, on_date=None) -> dict:
    """What a bill would be, without saving anything.

    cart: [{"variant_id", "qty", optional "discount_paise"}].
    Returns {"lines": [...], "calc": <core.tax.calculate_bill result>, "total_paise": int}.
    Each line has label, qty, unit_price_paise, discount_paise (item + share of bill discount),
    tax_rate_percent, line_total_paise and sellable (stock now). Raises ValueError with a message
    safe to show when something cannot be billed (inactive item, no price, bad quantity ...).
    """
    on = (on_date or date.today()).isoformat() if not isinstance(on_date, str) else on_date
    rules = load_tax_rules(conn) if mode == "regular" else []
    tax_lines, infos = [], []
    for item in cart:
        info = variant_info(conn, item.get("variant_id"))
        if info is None:
            raise ValueError("A product on this bill was not found.")
        if not info["is_active"]:
            raise ValueError(f"{info['label']} is inactive and cannot be sold.")
        price = info["price_paise"]
        if price <= 0:
            raise ValueError(f"{info['label']} has no selling price. Set the price first.")
        line = {
            "variant_id": info["variant_id"], "hsn": info["hsn"], "qty": item.get("qty"),
            "unit_price_paise": price, "discount_paise": item.get("discount_paise", 0),
        }
        if mode == "regular":
            if item.get("rate_percent") is not None:
                line["rate_percent"] = item["rate_percent"]
            elif info["gst_rate_percent"] is not None and not _has_rule_for(rules, info["hsn"], on):
                line["rate_percent"] = info["gst_rate_percent"]
        tax_lines.append(line)
        infos.append(info)

    calc = calculate_bill(
        tax_lines, on_date=on, rules=rules, mode=mode, prices_include_tax=prices_include_tax,
        bill_discount_paise=bill_discount_paise, intra_state=intra_state,
    )
    lines = []
    for info, line in zip(infos, calc["lines"]):
        lines.append({
            "variant_id": info["variant_id"],
            "label": info["label"],
            "qty": line["qty"],
            "unit_price_paise": line["unit_price_paise"],
            "discount_paise": line["item_discount_paise"] + line["bill_discount_share_paise"],
            "tax_rate_percent": line["rate_percent"],
            "line_total_paise": line["line_total_paise"],
            "sellable": info["sellable"],
        })
    return {"lines": lines, "calc": calc, "total_paise": calc["total_paise"]}
