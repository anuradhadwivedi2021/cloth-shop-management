"""PRD-06: warn about duplicate SKU or barcode before saving. Never merges anything.

The database already refuses two items with the same SKU or barcode. This lets a screen show a friendly
warning (which item already has the code) BEFORE the user presses Save, and also catches a supplier
barcode that equals another item's own barcode or SKU. A product NAME that looks the same is handled by
core.products.find_similar_products; a supplier design number needs its own field first (not built yet).
"""

from core.permissions import require_permission


def find_duplicate_codes(conn, user_id, *, sku=None, barcode=None, alt_barcode=None, exclude_variant_id=None) -> list:
    """Items that already use any of the typed codes.

    Each typed code is compared with the SKU, barcode and supplier barcode of every OTHER item
    (letters in any case). Returns [{'typed_as': 'sku'|'barcode'|'alt_barcode', 'code', 'found_in':
    'sku'|'barcode'|'alt_barcode', 'variant_id', 'product_id', 'product_name', 'size', 'colour'}].
    An empty list means no clash. Pass exclude_variant_id when editing an existing item.
    """
    require_permission(conn, user_id, "view_products", "see products")
    typed = []
    for field, value in (("sku", sku), ("barcode", barcode), ("alt_barcode", alt_barcode)):
        code = (value or "").strip()
        if code:
            typed.append((field, code))
    found, seen = [], set()
    for field, code in typed:
        rows = conn.execute(
            "SELECT v.id, v.product_id, p.name, v.size, v.colour, v.sku, v.barcode, v.alt_barcode "
            "FROM variants v JOIN products p ON p.id = v.product_id "
            "WHERE v.id IS NOT ? AND (upper(v.sku) = upper(?) OR upper(v.barcode) = upper(?) "
            "OR upper(v.alt_barcode) = upper(?)) ORDER BY v.id",
            (exclude_variant_id, code, code, code),
        ).fetchall()
        for row in rows:
            where = next(
                col for col in ("sku", "barcode", "alt_barcode")
                if (row[col] or "").upper() == code.upper()
            )
            key = (field, row["id"], where)
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "typed_as": field, "code": code, "found_in": where, "variant_id": row["id"],
                "product_id": row["product_id"], "product_name": row["name"],
                "size": row["size"], "colour": row["colour"],
            })
    return found