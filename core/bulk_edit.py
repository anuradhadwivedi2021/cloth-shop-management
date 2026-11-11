"""PRD-09 helpers for the Bulk Edit screen: find products to select, and turn typed text into a change.

The rules themselves (permissions, MRP ceiling, rounding, one transaction, audit) are in
core.product_edit.bulk_edit_products; this module only searches and reads what the person typed.
"""

from core import product_edit as pe
from core.permissions import require_permission
from core.product_detail import rupees_text
from core.products import search_products
from core.stock_ageing import aged_product_ids

MAX_SHOWN = 500
FIELD_NAMES = {"season": "Season", "gst_rate_percent": "GST rate", "reorder_level": "Reorder level",
               "default_price_paise": "Selling price"}


def find_products(conn, user_id, text="", *, limit=MAX_SHOWN, older_than_days=None) -> list:
    """Active products matching the text (name, brand, category, code ...).

    With older_than_days, only products whose every sellable piece has been on the shelf that long (STK-08).
    """
    require_permission(conn, user_id, "edit_products", "edit products")
    aged = None
    if older_than_days is not None:
        aged = set(aged_product_ids(conn, user_id, older_than_days))
    found = search_products(conn, text, limit=MAX_SHOWN if aged is not None else max(1, min(int(limit), MAX_SHOWN)))
    return [{"id": r["id"], "name": r["name"], "category": r["category"] or "", "season": r["season"] or "",
             "gst": r["gst_rate_percent"], "price": rupees_text(r["default_price_paise"])}
            for r in found if aged is None or r["id"] in aged][:MAX_SHOWN]


def parse_days(text):
    """Blank -> None (no age filter); otherwise a whole number of days."""
    if not (text or "").strip():
        return None
    try:
        days = int(text)
    except ValueError:
        raise ValueError("The number of days must be zero or more.") from None
    if days < 0:
        raise ValueError("The number of days must be zero or more.")
    return days


def build_changes(*, season=None, gst_text="", reorder_text="", price_mode="none", price_text="") -> dict:
    """Typed text -> the `changes` of bulk_edit_products. Blank means "leave as it is"."""
    changes = {}
    if season is not None and season.strip():
        changes["season"] = season.strip()
    if gst_text.strip():
        try:
            changes["gst_rate_percent"] = float(gst_text) if "." in gst_text else int(gst_text)
        except ValueError:
            raise ValueError("GST rate must be a number.") from None
    if reorder_text.strip():
        try:
            changes["reorder_level"] = int(reorder_text)
        except ValueError:
            raise ValueError("Reorder level must be a whole number.") from None
    if price_mode in ("set", "percent"):
        if not price_text.strip():
            raise ValueError("Type the new price or the percent.")
        try:
            if price_mode == "set":
                from decimal import Decimal
                paise = Decimal(price_text.strip()) * 100
                if paise != paise.to_integral_value():
                    raise ValueError
                changes["set_price_paise"] = int(paise)
            else:
                changes["price_change_percent"] = float(price_text)
        except (ValueError, ArithmeticError):
            raise ValueError("The price or percent is not a valid number.") from None
    if not changes:
        raise ValueError("Nothing to change.")
    return changes


def preview(conn, user_id, product_ids, changes) -> list:
    """Rows [{'name','field','old','new'}] for display; nothing is saved."""
    return _show(pe.bulk_edit_products(conn, user_id, product_ids, changes))


def apply(conn, user_id, product_ids, changes) -> dict:
    rows = pe.bulk_edit_products(conn, user_id, product_ids, changes, apply=True)
    return {"products": len({r["product_id"] for r in rows}), "changes": len(rows)}


def _show(rows):
    out = []
    for r in rows:
        money = r["field"] == "default_price_paise"
        fmt = (lambda v: rupees_text(v)) if money else (lambda v: "" if v is None else str(v))
        out.append({"name": r["name"], "field": FIELD_NAMES.get(r["field"], r["field"]),
                    "old": fmt(r["old"]), "new": fmt(r["new"])})
    return out
