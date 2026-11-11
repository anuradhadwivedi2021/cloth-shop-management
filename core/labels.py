"""Barcode labels (SRS STK-05 and PUR-05): what goes on each tag. No screens, no printers here.

This module decides WHAT a label says (shop name, product, size, colour, price, MRP, barcode) and turns
the barcode text into Code 128 bars. ui/print_service.py only draws that and hands it to the printer.

Rules:
  * Needs the "print_labels" permission (checked here, in the data layer).
  * The label shows the price the customer will really be billed: the variant's own price if it has one,
    otherwise the product's selling price. Cost price is never on a label.
  * The barcode is the variant's own barcode; a variant without one falls back to its SKU.
  * Printing is recorded in the audit log, but only AFTER the printer accepted the job (record_labels_printed).
"""

from core.audit import log_event
from core.permissions import require_permission
from core.shop_settings import get_preferences, get_shop_profile
from db.connection import transaction

# Label stock sizes in millimetres (width, height). Roll labels: one label per page.
LABEL_SIZES = {"50x25": (50.0, 25.0), "38x25": (38.0, 25.0)}
DEFAULT_SIZE = "50x25"
MAX_COPIES_PER_VARIANT = 500
MAX_LABELS_PER_JOB = 2000


# ---------------------------------------------------------------- barcode bars

def barcode_bars(text) -> list:
    """Code 128 bars for a text, as module widths that alternate bar, space, bar... starting with a bar.

    The first and last entries are bars, and the list already contains the start, check and stop symbols.
    A quiet zone (blank margin) of at least 10 modules is the printer's job. Numbers with an even number of
    digits (and EAN-13 style codes) use the compact Code C, so a 13-digit code is only 123 modules wide.
    """
    text = str(text or "")
    if not text or any(not 32 <= ord(c) < 127 for c in text):
        raise ValueError("A barcode can only hold plain letters, digits and common symbols.")
    from reportlab.graphics.barcode.code128 import Code128

    code = Code128(text, barWidth=1, humanReadable=False)
    code.validate()
    code.encode()
    code.decompose()
    # reportlab spells each run as a letter: A/a = 1 module, B/b = 2, C/c = 3, D/d = 4;
    # capital = bar, small = space.
    bars = []
    for i, ch in enumerate(code.decomposed):
        width = ord(ch.lower()) - ord("a") + 1
        if (i % 2 == 0) != ch.isupper():
            raise ValueError("The barcode could not be encoded.")
        bars.append(width)
    return bars


def barcode_width_modules(text) -> int:
    return sum(barcode_bars(text))


# ---------------------------------------------------------------- what a label says

def _clean_items(items) -> list:
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("Choose at least one item to print labels for.")
    merged = {}
    for item in items:
        variant_id = item.get("variant_id") if isinstance(item, dict) else None
        copies = item.get("copies", 1) if isinstance(item, dict) else None
        if not isinstance(variant_id, int) or isinstance(variant_id, bool):
            raise ValueError("Choose at least one item to print labels for.")
        if not isinstance(copies, int) or isinstance(copies, bool) or copies < 1:
            raise ValueError("The number of labels must be a whole number, at least 1.")
        if copies > MAX_COPIES_PER_VARIANT:
            raise ValueError(f"At most {MAX_COPIES_PER_VARIANT} labels of one item can be printed at a time.")
        merged[variant_id] = merged.get(variant_id, 0) + copies
    if any(c > MAX_COPIES_PER_VARIANT for c in merged.values()):
        raise ValueError(f"At most {MAX_COPIES_PER_VARIANT} labels of one item can be printed at a time.")
    if sum(merged.values()) > MAX_LABELS_PER_JOB:
        raise ValueError(f"At most {MAX_LABELS_PER_JOB} labels can be printed in one job.")
    return list(merged.items())


def label_settings(conn) -> dict:
    prefs = get_preferences(conn)
    size = prefs["label_size"]["value"]
    if size not in LABEL_SIZES:
        size = DEFAULT_SIZE
    return {"size": size, "show_shop_name": bool(prefs["label_show_shop_name"]["value"]),
            "show_mrp": bool(prefs["label_show_mrp"]["value"])}


def build_labels(conn, user_id, items) -> dict:
    """Everything the printer needs for a label job.

    items: [{'variant_id': int, 'copies': int}, ...]   (the same variant twice is added together)
    Returns {size, width_mm, height_mm, total, labels: [{variant_id, copies, shop, product, size, colour,
    price_paise, mrp_paise, barcode, sku, bars}]}.
    """
    require_permission(conn, user_id, "print_labels", "print barcode labels")
    wanted = _clean_items(items)
    cfg = label_settings(conn)
    width, height = LABEL_SIZES[cfg["size"]]
    shop = (get_shop_profile(conn) or {}).get("name") or ""
    labels = []
    for variant_id, copies in wanted:
        row = conn.execute(
            "SELECT v.id, v.size, v.colour, v.sku, v.barcode, v.price_override_paise, p.name, p.mrp_paise, "
            "p.default_price_paise FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?",
            (variant_id,),
        ).fetchone()
        if row is None:
            raise ValueError("One of the chosen items could not be found.")
        price = row["price_override_paise"] if row["price_override_paise"] is not None else row["default_price_paise"]
        code = row["barcode"] or row["sku"]
        labels.append({
            "variant_id": variant_id, "copies": copies,
            "shop": shop if cfg["show_shop_name"] else "",
            "product": row["name"], "size": row["size"], "colour": row["colour"],
            "price_paise": int(price), "mrp_paise": row["mrp_paise"] if cfg["show_mrp"] else None,
            "barcode": code, "sku": row["sku"], "bars": barcode_bars(code),
        })
    return {"size": cfg["size"], "width_mm": width, "height_mm": height,
            "total": sum(l["copies"] for l in labels), "labels": labels}


def record_labels_printed(conn, user_id, job) -> None:
    """Write the audit entry for a label job the printer accepted."""
    with transaction(conn):
        log_event(conn, "labels_printed", user_id=user_id, entity="labels",
                  after={"labels": job["total"], "size": job["size"],
                         "variants": [{"variant_id": l["variant_id"], "copies": l["copies"]} for l in job["labels"]]})
