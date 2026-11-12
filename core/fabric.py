"""PRD-11: fabric sold by the metre.

A product with unit 'metre' keeps stock and bill quantities in whole centimetres (2.5 m = 250). Its selling price is
per METRE, in whole rupees; the bill works with the price of one centimetre (rupees per metre = paise per cm), so the
tax, stock and return code needs no change. Everything shown to people is in metres.

  parse_metres("2.5") -> 250            format_metres(250) -> "2.5 m"           (whole centimetres only)
  is_metre(unit)                        cm_price(unit, price_paise)             price of 1 cm for a metre product
  check_cut(unit, cm, min_cut_cm)       refuse a cut shorter than the shop's minimum
  set_min_cut(conn, user_id, product_id, metres)
  receive_piece(conn, user_id, variant_id, kind, metres, ref=)   add a roll or a remnant to stock
  take_from_pieces(conn, variant_id, cm)     a sale's cut comes off remnants first, then the oldest roll
  list_pieces(conn, user_id, variant_id=None, only_open=True)
"""

from decimal import Decimal, InvalidOperation

from core.audit import log_event
from core.permissions import require_permission
from core.stock import add_movement
from db.connection import transaction

MAX_METRES = 10000


def is_metre(unit) -> bool:
    return unit == "metre"


def parse_metres(text, *, allow_zero=False) -> int:
    """'2.5' or '2,5' (metres) -> 250 (centimetres). At most 2 decimals, so every length is whole centimetres."""
    raw = str(text or "").strip().replace(",", ".").lower().removesuffix("m").strip()
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError("The length must be a number of metres, like 2.5") from None
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        raise ValueError("The length must be more than zero.")
    if value > MAX_METRES:
        raise ValueError(f"The length cannot be more than {MAX_METRES} metres.")
    cm = value * 100
    if cm != cm.to_integral_value():
        raise ValueError("The length can have at most 2 decimal places (whole centimetres).")
    return int(cm)


def format_metres(cm) -> str:
    cm = int(cm)
    metres, rest = divmod(abs(cm), 100)
    text = f"{metres}.{rest:02d}".rstrip("0").rstrip(".")
    return ("-" if cm < 0 else "") + text + " m"


def qty_text(unit, qty) -> str:
    """How a bill/stock quantity is shown: '250' as '2.5 m' for fabric, plain for everything else."""
    return format_metres(qty) if is_metre(unit) else str(qty)


def cm_price(unit, price_paise) -> int:
    """The price of ONE unit as the bill uses it. Fabric: price per metre (whole rupees) -> price of one centimetre."""
    if not is_metre(unit):
        return price_paise
    if price_paise % 100:
        raise ValueError("Fabric is priced per metre in whole rupees. Change the price to whole rupees.")
    return price_paise // 100


def metre_price(unit, unit_price_paise) -> int:
    """The price shown to people: fabric per metre, everything else as it is."""
    return unit_price_paise * 100 if is_metre(unit) else unit_price_paise


def check_cut(unit, cm, min_cut_cm) -> None:
    if is_metre(unit) and min_cut_cm and cm < min_cut_cm:
        raise ValueError(f"The shortest cut is {format_metres(min_cut_cm)}.")


def _product(conn, product_id):
    row = conn.execute("SELECT id, name, unit FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise ValueError("Product not found.")
    if not is_metre(row["unit"]):
        raise ValueError("This product is not sold by the metre. Set its unit to metre first.")
    return row


def set_min_cut(conn, user_id, product_id, metres) -> int:
    """The shortest length that will be cut (blank or 0 = no minimum). Returns centimetres."""
    require_permission(conn, user_id, "edit_products", "change the shortest cut")
    cm = 0 if str(metres or "").strip() in ("", "0") else parse_metres(metres)
    with transaction(conn):
        before = _product(conn, product_id)
        old = conn.execute("SELECT min_cut_cm FROM products WHERE id = ?", (product_id,)).fetchone()[0]
        conn.execute("UPDATE products SET min_cut_cm = ? WHERE id = ?", (cm, product_id))
        if old != cm:
            log_event(conn, "fabric_min_cut_changed", user_id=user_id, entity="product", entity_id=product_id,
                      before={"min_cut_cm": old}, after={"min_cut_cm": cm})
    return cm


def receive_piece(conn, user_id, variant_id, kind, metres, ref=None) -> int:
    """Put a roll or a remnant on the shelf: adds to stock and writes the piece down. Returns the piece id."""
    require_permission(conn, user_id, "adjust_stock", "add fabric stock")
    if kind not in ("roll", "remnant"):
        raise ValueError("A piece is a roll or a remnant.")
    cm = parse_metres(metres)
    ref = " ".join(str(ref or "").split())[:60] or None
    with transaction(conn):
        row = conn.execute("SELECT product_id FROM variants WHERE id = ?", (variant_id,)).fetchone()
        if row is None:
            raise ValueError("Variant not found.")
        _product(conn, row["product_id"])
        add_movement(conn, variant_id, cm, f"fabric {kind} received", doc_type="fabric_piece", user_id=user_id)
        cur = conn.execute("INSERT INTO fabric_pieces(variant_id, kind, ref, length_cm, remaining_cm, created_by) "
                           "VALUES (?,?,?,?,?,?)", (variant_id, kind, ref, cm, cm, user_id))
        log_event(conn, "fabric_piece_received", user_id=user_id, entity="fabric_piece", entity_id=cur.lastrowid,
                  after={"kind": kind, "length_cm": cm})
        return cur.lastrowid


def take_from_pieces(conn, variant_id, cm) -> None:
    """Record that `cm` was cut: remnants first (oldest first), then rolls (oldest first). Pieces that are not
    recorded (stock that was there before pieces were tracked) simply run out; the stock total is not touched here."""
    left = cm
    for piece in conn.execute("SELECT id, remaining_cm FROM fabric_pieces WHERE variant_id = ? AND remaining_cm > 0 "
                              "ORDER BY CASE kind WHEN 'remnant' THEN 0 ELSE 1 END, id", (variant_id,)).fetchall():
        if left <= 0:
            break
        take = min(left, piece["remaining_cm"])
        conn.execute("UPDATE fabric_pieces SET remaining_cm = remaining_cm - ? WHERE id = ?", (take, piece["id"]))
        left -= take


def list_pieces(conn, user_id, variant_id=None, only_open=True) -> list:
    require_permission(conn, user_id, "view_products", "see fabric pieces")
    sql = ("SELECT f.*, p.name AS product, v.size, v.colour FROM fabric_pieces f JOIN variants v ON v.id = f.variant_id "
           "JOIN products p ON p.id = v.product_id WHERE 1=1")
    args = []
    if variant_id is not None:
        sql += " AND f.variant_id = ?"
        args.append(variant_id)
    if only_open:
        sql += " AND f.remaining_cm > 0"
    return [dict(r) for r in conn.execute(sql + " ORDER BY f.id DESC", args)]


def metre_products(conn, user_id) -> list:
    """Active products sold by the metre, with their shortest cut and variants (for the Fabric screen)."""
    require_permission(conn, user_id, "view_products", "see fabric")
    out = []
    for p in conn.execute("SELECT id, name, default_price_paise, min_cut_cm FROM products WHERE unit = 'metre' "
                          "AND is_active = 1 ORDER BY name COLLATE NOCASE"):
        variants = [dict(v) for v in conn.execute(
            "SELECT v.id, v.size, v.colour, COALESCE((SELECT SUM(qty_change) FROM stock_movements m "
            "WHERE m.variant_id = v.id AND m.bucket = 'sellable'), 0) AS stock_cm FROM variants v "
            "WHERE v.product_id = ? AND v.is_active = 1 ORDER BY v.id", (p["id"],))]
        out.append({**dict(p), "variants": variants})
    return out
