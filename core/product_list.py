"""The product list for the Products screen (read only). Needs 'view_products'.

One row per product with how many sizes/colours it has and how many pieces can be sold. No cost is
shown here; changing anything goes through core.product_edit / core.product_detail.
"""

from core.permissions import require_permission


def list_products(conn, user_id, search="", *, include_inactive=False, limit=500) -> list:
    require_permission(conn, user_id, "view_products", "see products")
    like = f"%{(search or '').strip()}%"
    sql = (
        "SELECT p.id, p.name, p.category, p.brand, p.unit, p.default_price_paise AS price_paise, p.is_active, "
        "COUNT(v.id) AS variant_count, COALESCE(SUM(s.sellable_qty), 0) AS sellable_qty "
        "FROM products p LEFT JOIN variants v ON v.product_id = p.id AND v.is_active = 1 "
        "LEFT JOIN variant_stock s ON s.variant_id = v.id "
        "WHERE (p.name LIKE :q OR p.brand LIKE :q OR p.category LIKE :q OR p.hsn LIKE :q OR p.id IN ("
        "  SELECT product_id FROM variants WHERE sku LIKE :q OR barcode LIKE :q OR alt_barcode LIKE :q "
        "  OR size LIKE :q OR colour LIKE :q)) "
    )
    if not include_inactive:
        sql += "AND p.is_active = 1 "
    sql += "GROUP BY p.id ORDER BY p.name LIMIT :limit"
    return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, {"q": like, "limit": limit}).fetchall()]
