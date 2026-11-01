"""Adding and changing products and variants, with permissions and a full audit trail.

The screens must call these functions (not core.products directly), so the
rules hold even if a screen forgets to hide a button.

  add_product         create a product (+ optional size x colour variants), duplicate warning
  update_product      name, category, brand, HSN, GST rate, unit, reorder level ...
  change_prices       selling price, cost, MRP of a product      (needs 'change_price')
  set_variant_price   price override for one size/colour        (needs 'change_price')
  update_variant      supplier barcode, reorder level, rack/shelf location
  set_rack_location   same rack for many variants at once
  set_product_active / set_variant_active   hide from selling; history is never deleted
  bulk_edit_products  season, GST rate, reorder level or price for many products,
                      with a preview before anything is applied
  price_history       who changed which price, when, from what to what
  find_variants_by_location   search by rack or shelf

Price changes only affect FUTURE bills: a saved bill keeps the unit price it was
sold at (sale_lines), and purchases keep their own cost. Every change is written
to the audit log with the user, the time, and the before and after values.
"""

import json
from decimal import ROUND_HALF_UP, Decimal

from core.audit import log_event
from core.permissions import PermissionDenied, has_permission, require_permission
from core.products import create_product, find_similar_products, generate_variants
from db.connection import transaction

UNITS = ("piece", "pair", "set", "metre")
PRODUCT_FIELDS = {
    "name", "category", "brand", "hsn", "material", "season", "description",
    "gst_rate_percent", "unit", "reorder_level",
}
PRICE_KEYS = {"price_paise": "default_price_paise", "cost_paise": "default_cost_paise", "mrp_paise": "mrp_paise"}
PRICE_LABELS = {"price_paise": "Selling price", "cost_paise": "Cost", "mrp_paise": "MRP"}
VARIANT_FIELDS = {"alt_barcode", "reorder_level", "rack_location"}
BULK_KEYS = {"season", "gst_rate_percent", "reorder_level", "set_price_paise", "price_change_percent"}


class DuplicateProductError(ValueError):
    """A product with the same name already exists. Nothing was saved."""

    def __init__(self, existing_ids):
        self.existing_ids = list(existing_ids)
        super().__init__("A product with this name already exists. Review it, or confirm to add anyway.")


# ---------------------------------------------------------------- validation

def _paise(value, label, *, allow_none=False):
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{label} is required.")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a whole number of paise, zero or more.")
    return value


def _text(value):
    value = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    return value or None


def _gst(value):
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("GST rate must be a number.") from None
    if not 0 <= value <= 100:
        raise ValueError("GST rate must be between 0 and 100.")
    return value


def _reorder(value, *, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Reorder level must be a whole number, zero or more.")
    return value


def _unit(value):
    if value not in UNITS:
        raise ValueError(f"Unit must be one of {', '.join(UNITS)}.")
    return value


def _check_mrp(price, mrp, label="Selling price"):
    if mrp is not None and price is not None and price > mrp:
        raise ValueError(f"{label} cannot be above the MRP.")


def _product(conn, product_id):
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise ValueError("Product not found.")
    return row


def _variant(conn, variant_id):
    row = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if row is None:
        raise ValueError("Variant not found.")
    return row


def _sellable(conn, where, arg):
    row = conn.execute(
        f"SELECT COALESCE(SUM(s.sellable_qty), 0) FROM variant_stock s "
        f"JOIN variants v ON v.id = s.variant_id WHERE {where} = ?", (arg,),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------- add

def add_product(conn, user_id, name, *, sizes=None, colours=None, allow_duplicate=False, **fields) -> dict:
    """Create a product, and its size x colour variants when sizes and colours are given.

    If a product with the same name exists, raises DuplicateProductError (with the
    existing ids) unless allow_duplicate=True. Products are never merged.
    Returns {'product_id': ..., 'variant_ids': [...]}.
    """
    require_permission(conn, user_id, "edit_products", "add products")
    allowed = PRODUCT_FIELDS - {"name"} | {"default_price_paise", "default_cost_paise", "mrp_paise"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown product field: {', '.join(sorted(unknown))}.")
    name = (name or "").strip()
    if not name:
        raise ValueError("Product name is required.")
    clean = {k: _text(v) for k, v in fields.items() if k in ("category", "brand", "hsn", "material", "season", "description")}
    if "gst_rate_percent" in fields:
        clean["gst_rate_percent"] = _gst(fields["gst_rate_percent"])
    if "unit" in fields:
        clean["unit"] = _unit(fields["unit"])
    if "reorder_level" in fields:
        clean["reorder_level"] = _reorder(fields["reorder_level"])
    price = _paise(fields.get("default_price_paise", 0), "Selling price")
    cost = _paise(fields.get("default_cost_paise", 0), "Cost")
    mrp = _paise(fields.get("mrp_paise"), "MRP", allow_none=True)
    _check_mrp(price, mrp)
    if (price or cost or mrp) and not has_permission(conn, user_id, "change_price"):
        raise PermissionDenied("You are not allowed to set prices. Add the product without prices.")
    clean.update(default_price_paise=price, default_cost_paise=cost, mrp_paise=mrp)

    if not allow_duplicate:
        similar = find_similar_products(conn, name)
        if similar:
            raise DuplicateProductError([r["id"] for r in similar])

    product_id = create_product(conn, name, **clean)
    variant_ids = generate_variants(conn, product_id, sizes, colours) if (sizes and colours) else []
    with transaction(conn):
        log_event(conn, "product_created", user_id=user_id, entity="product", entity_id=product_id,
                  after={"name": name, "variants": len(variant_ids)})
    return {"product_id": product_id, "variant_ids": variant_ids}


# ---------------------------------------------------------------- edit

def update_product(conn, user_id, product_id, **changes) -> list:
    """Change details of a product. Returns the names of the fields that actually changed."""
    if set(changes) & (set(PRICE_KEYS) | set(PRICE_KEYS.values())):
        raise ValueError("Use change_prices to change prices.")
    unknown = set(changes) - PRODUCT_FIELDS
    if unknown:
        raise ValueError(f"Unknown product field: {', '.join(sorted(unknown))}.")
    if not changes:
        return []

    new = {}
    for key, value in changes.items():
        if key == "name":
            value = (value or "").strip()
            if not value:
                raise ValueError("Product name is required.")
        elif key == "gst_rate_percent":
            value = _gst(value)
        elif key == "unit":
            value = _unit(value)
        elif key == "reorder_level":
            value = _reorder(value)
        else:
            value = _text(value)
        new[key] = value

    with transaction(conn):
        require_permission(conn, user_id, "edit_products", "edit products")
        row = _product(conn, product_id)
        if "unit" in new and new["unit"] != row["unit"]:
            used = conn.execute(
                "SELECT 1 FROM stock_movements m JOIN variants v ON v.id = m.variant_id "
                "WHERE v.product_id = ? LIMIT 1", (product_id,),
            ).fetchone()
            if used:
                raise ValueError("The unit cannot be changed after stock has been recorded for this product.")
        diff = {k: v for k, v in new.items() if row[k] != v}
        if not diff:
            return []
        sets = ", ".join(f"{k} = ?" for k in diff)
        conn.execute(f"UPDATE products SET {sets} WHERE id = ?", (*diff.values(), product_id))
        log_event(conn, "product_updated", user_id=user_id, entity="product", entity_id=product_id,
                  before={k: row[k] for k in diff}, after=diff)
        return sorted(diff)


def change_prices(conn, user_id, product_id, **prices) -> list:
    """Change price_paise, cost_paise and/or mrp_paise (mrp_paise=None clears the MRP).

    Only future bills use the new price; saved bills and purchases are never rewritten.
    Returns the names of the prices that actually changed.
    """
    unknown = set(prices) - set(PRICE_KEYS)
    if unknown:
        raise ValueError(f"Unknown price: {', '.join(sorted(unknown))}. Use price_paise, cost_paise or mrp_paise.")
    if not prices:
        return []
    new = {}
    for key, value in prices.items():
        new[PRICE_KEYS[key]] = _paise(value, PRICE_LABELS[key], allow_none=(key == "mrp_paise"))

    with transaction(conn):
        require_permission(conn, user_id, "change_price", "change prices")
        row = _product(conn, product_id)
        final_price = new.get("default_price_paise", row["default_price_paise"])
        final_mrp = new["mrp_paise"] if "mrp_paise" in new else row["mrp_paise"]
        _check_mrp(final_price, final_mrp)
        diff = {k: v for k, v in new.items() if row[k] != v}
        if not diff:
            return []
        sets = ", ".join(f"{k} = ?" for k in diff)
        conn.execute(f"UPDATE products SET {sets} WHERE id = ?", (*diff.values(), product_id))
        log_event(conn, "price_changed", user_id=user_id, entity="product", entity_id=product_id,
                  before={k: row[k] for k in diff}, after=diff)
        inverse = {v: k for k, v in PRICE_KEYS.items()}
        return sorted(inverse[k] for k in diff)


def set_variant_price(conn, user_id, variant_id, price_paise) -> bool:
    """Give one size/colour its own price (None removes it, so the product price applies). True if changed."""
    price_paise = _paise(price_paise, "Price", allow_none=True)
    with transaction(conn):
        require_permission(conn, user_id, "change_price", "change prices")
        row = _variant(conn, variant_id)
        mrp = _product(conn, row["product_id"])["mrp_paise"]
        _check_mrp(price_paise, mrp)
        if row["price_override_paise"] == price_paise:
            return False
        conn.execute("UPDATE variants SET price_override_paise = ? WHERE id = ?", (price_paise, variant_id))
        log_event(conn, "price_changed", user_id=user_id, entity="variant", entity_id=variant_id,
                  before={"price_override_paise": row["price_override_paise"]},
                  after={"price_override_paise": price_paise})
        return True


def _clean_variant_changes(conn, variant_row, changes):
    unknown = set(changes) - VARIANT_FIELDS
    if unknown:
        raise ValueError(f"Unknown variant field: {', '.join(sorted(unknown))}.")
    new = {}
    for key, value in changes.items():
        if key == "reorder_level":
            value = _reorder(value, allow_none=True)
        else:
            value = _text(value)
        if key == "alt_barcode" and value is not None:
            clash = conn.execute(
                "SELECT 1 FROM variants WHERE id != ? AND (barcode = ? OR alt_barcode = ? OR upper(sku) = upper(?))",
                (variant_row["id"], value, value, value),
            ).fetchone()
            if clash or value in (variant_row["barcode"], variant_row["sku"]):
                raise ValueError("This code is already used by another item.")
        new[key] = value
    return new


def update_variant(conn, user_id, variant_id, **changes) -> list:
    """Change alt_barcode (supplier barcode), reorder_level or rack_location. None or blank clears it."""
    if not changes:
        return []
    with transaction(conn):
        require_permission(conn, user_id, "edit_products", "edit products")
        row = _variant(conn, variant_id)
        new = _clean_variant_changes(conn, row, changes)
        diff = {k: v for k, v in new.items() if row[k] != v}
        if not diff:
            return []
        sets = ", ".join(f"{k} = ?" for k in diff)
        conn.execute(f"UPDATE variants SET {sets} WHERE id = ?", (*diff.values(), variant_id))
        log_event(conn, "variant_updated", user_id=user_id, entity="variant", entity_id=variant_id,
                  before={k: row[k] for k in diff}, after=diff)
        return sorted(diff)


def set_rack_location(conn, user_id, variant_ids, rack) -> int:
    """Put many variants on the same rack/shelf (blank clears it). Returns how many changed. All or nothing."""
    rack = _text(rack)
    changed = 0
    with transaction(conn):
        require_permission(conn, user_id, "edit_products", "edit products")
        rows = [_variant(conn, vid) for vid in variant_ids]
        for row in rows:
            if row["rack_location"] == rack:
                continue
            conn.execute("UPDATE variants SET rack_location = ? WHERE id = ?", (rack, row["id"]))
            log_event(conn, "variant_updated", user_id=user_id, entity="variant", entity_id=row["id"],
                      before={"rack_location": row["rack_location"]}, after={"rack_location": rack})
            changed += 1
    return changed


# ---------------------------------------------------------------- active / inactive

def set_product_active(conn, user_id, product_id, active: bool) -> dict:
    """Hide a product from selling (or show it again). History is kept.

    Returns {'changed': bool, 'sellable_stock_left': n}; the screen can warn when stock is left.
    """
    with transaction(conn):
        require_permission(conn, user_id, "edit_products", "edit products")
        row = _product(conn, product_id)
        left = _sellable(conn, "v.product_id", product_id)
        if bool(row["is_active"]) == bool(active):
            return {"changed": False, "sellable_stock_left": left}
        conn.execute("UPDATE products SET is_active = ? WHERE id = ?", (1 if active else 0, product_id))
        log_event(conn, "product_activated" if active else "product_deactivated", user_id=user_id,
                  entity="product", entity_id=product_id, after={"sellable_stock_left": left})
        return {"changed": True, "sellable_stock_left": left}


def set_variant_active(conn, user_id, variant_id, active: bool) -> dict:
    """Same as set_product_active, for one size/colour."""
    with transaction(conn):
        require_permission(conn, user_id, "edit_products", "edit products")
        row = _variant(conn, variant_id)
        left = _sellable(conn, "v.id", variant_id)
        if bool(row["is_active"]) == bool(active):
            return {"changed": False, "sellable_stock_left": left}
        conn.execute("UPDATE variants SET is_active = ? WHERE id = ?", (1 if active else 0, variant_id))
        log_event(conn, "variant_activated" if active else "variant_deactivated", user_id=user_id,
                  entity="variant", entity_id=variant_id, after={"sellable_stock_left": left})
        return {"changed": True, "sellable_stock_left": left}


# ---------------------------------------------------------------- bulk edit

def _percent_price(old_paise, percent):
    """Old price changed by a percent, rounded half-up to a whole rupee."""
    rupees = Decimal(old_paise) * (Decimal(100) + Decimal(str(percent))) / Decimal(100) / Decimal(100)
    return int(rupees.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 100


def bulk_edit_products(conn, user_id, product_ids, changes, *, apply=False) -> list:
    """Change season, GST rate, reorder level or price for many products at once.

    changes may contain:
      season, gst_rate_percent, reorder_level
      set_price_paise         one new selling price for all
      price_change_percent    e.g. 10 for +10 %, -15 for -15 % (rounded to a whole rupee)
    With apply=False (the default) nothing is saved: you get the preview rows
    [{'product_id', 'name', 'field', 'old', 'new'}]. Call again with apply=True to save.
    Price overrides on single variants are not touched. All or nothing.
    """
    unknown = set(changes) - BULK_KEYS
    if unknown:
        raise ValueError(f"Unknown bulk change: {', '.join(sorted(unknown))}.")
    if not changes:
        raise ValueError("Nothing to change.")
    if "set_price_paise" in changes and "price_change_percent" in changes:
        raise ValueError("Use either a new price or a percent change, not both.")
    ids = list(dict.fromkeys(product_ids or []))
    if not ids:
        raise ValueError("Select at least one product.")

    new_values = {}
    if "season" in changes:
        new_values["season"] = _text(changes["season"])
    if "gst_rate_percent" in changes:
        new_values["gst_rate_percent"] = _gst(changes["gst_rate_percent"])
    if "reorder_level" in changes:
        new_values["reorder_level"] = _reorder(changes["reorder_level"])
    if "set_price_paise" in changes:
        _paise(changes["set_price_paise"], "Price")
    if "price_change_percent" in changes:
        pct = changes["price_change_percent"]
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not -90 <= pct <= 500:
            raise ValueError("Price change must be a number between -90 and 500 percent.")
    price_change = "set_price_paise" in changes or "price_change_percent" in changes

    def build():
        require_permission(conn, user_id, "edit_products", "edit products")
        if price_change:
            require_permission(conn, user_id, "change_price", "change prices")
        rows, plan = [], []
        for pid in ids:
            p = _product(conn, pid)
            diff = {k: v for k, v in new_values.items() if p[k] != v}
            if "set_price_paise" in changes:
                target = changes["set_price_paise"]
            elif "price_change_percent" in changes:
                target = _percent_price(p["default_price_paise"], changes["price_change_percent"])
            else:
                target = p["default_price_paise"]
            if target != p["default_price_paise"]:
                if p["mrp_paise"] is not None and target > p["mrp_paise"]:
                    raise ValueError(f"The new price of '{p['name']}' would be above its MRP.")
                diff["default_price_paise"] = target
            for field, value in diff.items():
                rows.append({"product_id": pid, "name": p["name"], "field": field, "old": p[field], "new": value})
            if diff:
                plan.append((pid, p, diff))
        return rows, plan

    if not apply:
        return build()[0]

    with transaction(conn):
        rows, plan = build()
        for pid, p, diff in plan:
            sets = ", ".join(f"{k} = ?" for k in diff)
            conn.execute(f"UPDATE products SET {sets} WHERE id = ?", (*diff.values(), pid))
            price_part = {k: v for k, v in diff.items() if k == "default_price_paise"}
            other = {k: v for k, v in diff.items() if k != "default_price_paise"}
            if price_part:
                log_event(conn, "price_changed", user_id=user_id, entity="product", entity_id=pid,
                          before={"default_price_paise": p["default_price_paise"]}, after=price_part)
            if other:
                log_event(conn, "product_updated", user_id=user_id, entity="product", entity_id=pid,
                          before={k: p[k] for k in other}, after=other)
        return rows


# ---------------------------------------------------------------- history and search

def price_history(conn, user_id, *, product_id=None, variant_id=None) -> list:
    """Every price change of a product or a variant, newest first. Needs 'change_price' or 'view_cost'."""
    if not (has_permission(conn, user_id, "change_price") or has_permission(conn, user_id, "view_cost")):
        raise PermissionDenied("You are not allowed to see price history.")
    if (product_id is None) == (variant_id is None):
        raise ValueError("Give either a product or a variant.")
    entity, entity_id = ("product", product_id) if product_id is not None else ("variant", variant_id)
    rows = conn.execute(
        "SELECT a.created_at, u.username, a.before_json, a.after_json FROM audit_log a "
        "LEFT JOIN users u ON u.id = a.user_id "
        "WHERE a.action = 'price_changed' AND a.entity = ? AND a.entity_id = ? ORDER BY a.id DESC",
        (entity, entity_id),
    ).fetchall()
    return [
        {"when": r["created_at"], "user": r["username"],
         "before": json.loads(r["before_json"]) if r["before_json"] else None,
         "after": json.loads(r["after_json"]) if r["after_json"] else None}
        for r in rows
    ]


def find_variants_by_location(conn, user_id, text, *, limit=100) -> list:
    """Find variants by rack or shelf text, with their sellable stock."""
    require_permission(conn, user_id, "view_products", "see products")
    text = (text or "").strip()
    if not text:
        return []
    return conn.execute(
        "SELECT v.id AS variant_id, p.name, v.size, v.colour, v.rack_location, s.sellable_qty "
        "FROM variants v JOIN products p ON p.id = v.product_id JOIN variant_stock s ON s.variant_id = v.id "
        "WHERE v.rack_location LIKE ? ORDER BY v.rack_location, p.name, v.size, v.colour LIMIT ?",
        (f"%{text}%", limit),
    ).fetchall()
