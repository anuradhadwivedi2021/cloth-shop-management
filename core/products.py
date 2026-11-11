"""Products and size x colour variants.

Stock is NOT stored here. Stock lives only in stock_movements (see variant_stock view).
"""

import re

from db.connection import transaction


# ---------- barcode helpers ----------

def ean13_check_digit(first12: str) -> str:
    """Return the EAN-13 check digit for a 12-digit string."""
    if len(first12) != 12 or not first12.isdigit():
        raise ValueError("EAN-13 base must be exactly 12 digits.")
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def make_internal_barcode(variant_id: int) -> str:
    """Internal in-store EAN-13 barcode (prefix 2) derived from the variant id."""
    base = "2" + f"{variant_id:011d}"
    return base + ean13_check_digit(base)


# ---------- small helpers ----------

def _clean(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _normalise(values, label: str):
    """Strip, drop blanks, remove case-insensitive duplicates (order kept)."""
    seen = set()
    result = []
    for value in values or []:
        text = str(value).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        result.append(text)
    if not result:
        raise ValueError(f"At least one {label} is required.")
    return result


def _unique_sku(conn, product_id: int, size: str, colour: str) -> str:
    base = f"P{product_id:04d}-{_clean(size)}-{_clean(colour)}"
    candidate = base
    n = 2
    while conn.execute("SELECT 1 FROM variants WHERE sku = ?", (candidate,)).fetchone():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


# ---------- products ----------

def create_product(
    conn,
    name,
    *,
    category=None,
    brand=None,
    hsn=None,
    material=None,
    season=None,
    description=None,
    gst_rate_percent=None,
    mrp_paise=None,
    default_cost_paise=0,
    default_price_paise=0,
    unit="piece",
    reorder_level=0,
):
    """Create a product (no variants yet). Returns the new product id."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Product name is required.")
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO products(name, category, brand, hsn, material, season, description, "
            "gst_rate_percent, mrp_paise, default_cost_paise, default_price_paise, unit, reorder_level) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, category, brand, hsn, material, season, description,
             gst_rate_percent, mrp_paise, default_cost_paise, default_price_paise,
             unit, reorder_level),
        )
        return cur.lastrowid


def find_similar_products(conn, name):
    """Possible duplicates (same name, ignoring case). Never merges anything."""
    return conn.execute(
        "SELECT * FROM products WHERE lower(trim(name)) = lower(trim(?)) ORDER BY id",
        (name or "",),
    ).fetchall()


def search_products(conn, text, *, active_only=True, limit=50):
    """Search by product name, brand, category, HSN, or any variant's SKU, barcode, size, colour."""
    like = f"%{(text or '').strip()}%"
    sql = (
        "SELECT DISTINCT p.* FROM products p "
        "LEFT JOIN variants v ON v.product_id = p.id "
        "WHERE (p.name LIKE :q OR p.brand LIKE :q OR p.category LIKE :q OR p.hsn LIKE :q "
        "OR v.sku LIKE :q OR v.barcode LIKE :q OR v.alt_barcode LIKE :q "
        "OR v.size LIKE :q OR v.colour LIKE :q) "
    )
    if active_only:
        sql += "AND p.is_active = 1 "
    sql += "ORDER BY p.name LIMIT :limit"
    return conn.execute(sql, {"q": like, "limit": limit}).fetchall()


# ---------- variants ----------

def generate_variants(conn, product_id, sizes, colours):
    """Create every size x colour combination for a product in one action.

    Existing combinations are skipped (never merged or overwritten).
    Returns the list of NEW variant ids.
    """
    sizes = _normalise(sizes, "size")
    colours = _normalise(colours, "colour")

    with transaction(conn):
        if conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone() is None:
            raise ValueError("Product not found.")

        created = []
        for size in sizes:
            for colour in colours:
                exists = conn.execute(
                    "SELECT 1 FROM variants WHERE product_id = ? "
                    "AND lower(size) = lower(?) AND lower(colour) = lower(?)",
                    (product_id, size, colour),
                ).fetchone()
                if exists:
                    continue
                sku = _unique_sku(conn, product_id, size, colour)
                cur = conn.execute(
                    "INSERT INTO variants(product_id, size, colour, sku) VALUES (?,?,?,?)",
                    (product_id, size, colour, sku),
                )
                variant_id = cur.lastrowid
                conn.execute(
                    "UPDATE variants SET barcode = ? WHERE id = ?",
                    (make_internal_barcode(variant_id), variant_id),
                )
                created.append(variant_id)
        return created


def find_variant_by_code(conn, code):
    """Find a variant by barcode, alternate (supplier) barcode, or SKU."""
    code = (code or "").strip()
    if not code:
        return None
    # one indexed lookup at a time (an OR across columns makes SQLite read the whole table)
    for where in ("barcode = ?", "alt_barcode = ?", "upper(sku) = upper(?)"):
        found = conn.execute(f"SELECT * FROM variants WHERE {where} LIMIT 1", (code,)).fetchone()
        if found:
            return found
    return None


def list_variants_with_stock(conn, product_id):
    """All variants of a product with current stock per bucket (from stock movements)."""
    return conn.execute(
        "SELECT v.*, s.sellable_qty, s.damaged_qty, s.returned_pending_qty, "
        "s.on_trial_qty, s.reserved_qty "
        "FROM variants v JOIN variant_stock s ON s.variant_id = v.id "
        "WHERE v.product_id = ? ORDER BY v.size, v.colour",
        (product_id,),
    ).fetchall()

