"""Everything the product detail screen needs, so the screen runs no SQL and holds no rules.

  get_product_detail   one product with all its variants, stock, prices and what this user may do
  preview_new_variants which size x colour combinations are new and which already exist
  add_variants         add more sizes / colours to an existing product (one transaction, audited)
  save_changes         save edited details and prices, and say exactly what was saved

Changing details, prices, rack, active/inactive and a variant's own fields stays in core.product_edit;
this module only adds what that one does not have. Cost is shown only to users with 'view_cost'.
Money is whole paise.
"""

from core.audit import log_event
from core.matrix_entry import preview_matrix
from core.money import parse_rupees
from core.permissions import PermissionDenied, has_permission, require_permission
from core.product_edit import PRICE_KEYS, UNITS, change_prices, update_product
from core.products import _unique_sku, make_internal_barcode
from db.connection import transaction

DETAIL_FIELDS = ("name", "category", "brand", "hsn", "material", "season", "description")


def rupees_text(paise) -> str:
    """Paise as plain rupees for an edit box: 105050 -> '1050.50'. None -> ''. (Integer maths, no floats.)"""
    if paise is None:
        return ""
    return f"{paise // 100}.{paise % 100:02d}"


def _product_row(conn, product_id):
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise ValueError("Product not found.")
    return row


def get_product_detail(conn, user_id, product_id) -> dict:
    """The product, its variants (oldest first) and totals. Needs 'view_products'."""
    require_permission(conn, user_id, "view_products", "see products")
    row = _product_row(conn, product_id)
    can_cost = has_permission(conn, user_id, "view_cost")
    product = {
        "id": row["id"], "name": row["name"], "category": row["category"], "brand": row["brand"],
        "hsn": row["hsn"], "material": row["material"], "season": row["season"],
        "description": row["description"], "gst_rate_percent": row["gst_rate_percent"],
        "unit": row["unit"], "reorder_level": row["reorder_level"], "is_active": bool(row["is_active"]),
        "price_paise": row["default_price_paise"], "mrp_paise": row["mrp_paise"],
        "cost_paise": row["default_cost_paise"] if can_cost else None,
    }
    variants = []
    for v in conn.execute(
        "SELECT v.*, s.sellable_qty, s.damaged_qty, s.returned_pending_qty "
        "FROM variants v JOIN variant_stock s ON s.variant_id = v.id WHERE v.product_id = ? ORDER BY v.id",
        (product_id,),
    ):
        override = v["price_override_paise"]
        variants.append({
            "id": v["id"], "size": v["size"], "colour": v["colour"], "sku": v["sku"], "barcode": v["barcode"],
            "alt_barcode": v["alt_barcode"], "rack_location": v["rack_location"],
            "reorder_level": v["reorder_level"], "price_override_paise": override,
            "price_paise": override if override is not None else row["default_price_paise"],
            "is_active": bool(v["is_active"]), "sellable_qty": v["sellable_qty"],
            "damaged_qty": v["damaged_qty"], "returned_pending_qty": v["returned_pending_qty"],
        })
    return {
        "product": product,
        "variants": variants,
        "totals": {
            "variants": len(variants),
            "active_variants": sum(1 for v in variants if v["is_active"]),
            "sellable_qty": sum(v["sellable_qty"] for v in variants),
        },
        "can": {
            "edit": has_permission(conn, user_id, "edit_products"),
            "change_price": has_permission(conn, user_id, "change_price"),
            "view_cost": can_cost,
        },
        "units": list(UNITS),
    }


def _existing_pairs(conn, product_id) -> set:
    return {
        (r["size"].lower(), r["colour"].lower())
        for r in conn.execute("SELECT size, colour FROM variants WHERE product_id = ?", (product_id,))
    }


def preview_new_variants(conn, user_id, product_id, sizes_text, colours_text) -> dict:
    """{'new': [(size, colour)...], 'existing': [...]} for what was typed. Writes nothing."""
    require_permission(conn, user_id, "edit_products", "edit products")
    _product_row(conn, product_id)
    have = _existing_pairs(conn, product_id)
    result = {"new": [], "existing": []}
    for size, colour in preview_matrix(sizes_text, colours_text):
        result["existing" if (size.lower(), colour.lower()) in have else "new"].append((size, colour))
    return result


def add_variants(conn, user_id, product_id, sizes_text, colours_text) -> dict:
    """Add the new size x colour combinations to a product. Existing ones are never touched.

    One transaction with the audit entry. Raises ValueError (nothing saved) when every combination
    already exists. Returns {'variant_ids': [...], 'skipped': n}.
    """
    combos = preview_matrix(sizes_text, colours_text)  # validates before anything is written
    with transaction(conn):
        require_permission(conn, user_id, "edit_products", "edit products")
        _product_row(conn, product_id)
        have = _existing_pairs(conn, product_id)
        created, skipped = [], 0
        for size, colour in combos:
            if (size.lower(), colour.lower()) in have:
                skipped += 1
                continue
            have.add((size.lower(), colour.lower()))
            variant_id = conn.execute(
                "INSERT INTO variants(product_id, size, colour, sku) VALUES (?,?,?,?)",
                (product_id, size, colour, _unique_sku(conn, product_id, size, colour)),
            ).lastrowid
            conn.execute("UPDATE variants SET barcode = ? WHERE id = ?", (make_internal_barcode(variant_id), variant_id))
            created.append(variant_id)
        if not created:
            raise ValueError("All of these sizes and colours already exist.")
        log_event(conn, "variants_added", user_id=user_id, entity="product", entity_id=product_id,
                  after={"variant_ids": created, "skipped_existing": skipped})
    return {"variant_ids": created, "skipped": skipped}


def _price_text(text, label, *, blank_is_none=False):
    if not (text or "").strip():
        return None if blank_is_none else 0
    try:
        return parse_rupees(text)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


def save_changes(conn, user_id, product_id, *, fields=None, prices=None, gst_text=None, reorder_text=None,
                 unit=None) -> dict:
    """Save what the screen edited. Only values that really changed are sent to core.product_edit.

    fields:   {name, category, brand, hsn, material, season, description: text}
    prices:   {'price': text, 'cost': text, 'mrp': text}   (rupees as typed; blank MRP clears it)
    gst_text, reorder_text, unit: as typed.

    Everything is checked first; nothing is written if any text is invalid. Details and prices are
    two separate saves (two permissions). If the details were saved but the prices were refused, the
    result says so: {'details': [...], 'prices': [...], 'error': None | message}.
    Raises (nothing saved) when the details step fails.
    """
    row = _product_row(conn, product_id)
    changes = {}
    for key in DETAIL_FIELDS:
        if fields is not None and key in fields:
            new = (fields[key] or "").strip()
            if key == "name" and not new:
                raise ValueError("Product name is required.")
            if (new or None) != ((row[key] or "").strip() or None):
                changes[key] = new
    if gst_text is not None:
        text = gst_text.strip()
        if text:
            try:
                new_gst = float(text)
            except ValueError:
                raise ValueError("GST rate must be a number.") from None
        else:
            new_gst = None
        if new_gst != row["gst_rate_percent"]:
            changes["gst_rate_percent"] = text if text else None
    if reorder_text is not None:
        text = reorder_text.strip() or "0"
        if not text.isdigit():
            raise ValueError("Reorder level must be a whole number, zero or more.")
        if int(text) != row["reorder_level"]:
            changes["reorder_level"] = int(text)
    if unit is not None and unit != row["unit"]:
        changes["unit"] = unit

    new_prices = {}
    if prices is not None:
        parsed = {
            "price_paise": _price_text(prices.get("price"), "Selling price") if "price" in prices else None,
            "cost_paise": _price_text(prices.get("cost"), "Cost") if prices.get("cost") is not None else None,
            "mrp_paise": _price_text(prices.get("mrp"), "MRP", blank_is_none=True) if "mrp" in prices else "skip",
        }
        for key, value in parsed.items():
            if value == "skip" or (value is None and key != "mrp_paise"):
                continue
            if value != row[PRICE_KEYS[key]]:
                new_prices[key] = value

    result = {"details": [], "prices": [], "error": None}
    if changes:
        result["details"] = update_product(conn, user_id, product_id, **changes)
    if new_prices:
        try:
            result["prices"] = change_prices(conn, user_id, product_id, **new_prices)
        except (ValueError, PermissionDenied) as exc:
            if not result["details"]:
                raise
            result["error"] = str(exc)
    return result