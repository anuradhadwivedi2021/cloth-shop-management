"""PRD-10: import products and variants from a CSV or Excel file.

One row of the file is ONE variant (a size and colour of a product). Rows with the same product name make one
product with many variants. The steps the owner sees:

  1. preview_import(...)   reads the file and checks EVERY row. Nothing is saved. The result says how many
                           products and variants would be made, and lists each problem with its row number.
  2. write_error_file(...) saves the problems as a CSV (the original columns plus the reason) to fix and retry.
  3. import_products(...)  saves everything in ONE transaction. If even one row has a problem nothing is
                           saved: there is never a half import. It checks the file again itself and never
                           trusts an earlier preview.

Rules: a variant that already exists is an error, never merged or overwritten. A product name that already
exists gets the NEW variants added to it (its own details are left alone). Money in the file is rupees.
Cost needs the "see cost" permission and opening stock needs the "adjust stock" permission.
"""

import csv
import os
import re
from decimal import Decimal, InvalidOperation

from core.audit import log_event
from core.money import parse_rupees
from core.permissions import has_permission, require_permission
from core.products import make_internal_barcode
from core.stock import add_movement
from db.connection import transaction

MAX_ROWS = 20_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_STOCK = 1_000_000
MAX_TEXT = 200

# the columns, with the header names a file may use for each (compared in lower case, ignoring spaces and _ -)
COLUMNS = {
    "name": ("name", "product", "productname", "item", "itemname"),
    "size": ("size",),
    "colour": ("colour", "color"),
    "category": ("category",),
    "brand": ("brand",),
    "hsn": ("hsn", "hsncode"),
    "gst": ("gst", "gstrate", "gst%", "gstpercent"),
    "mrp": ("mrp",),
    "price": ("price", "sellingprice", "saleprice", "rate"),
    "cost": ("cost", "costprice", "purchaseprice"),
    "sku": ("sku", "itemcode", "code"),
    "barcode": ("barcode",),
    "alt_barcode": ("altbarcode", "supplierbarcode", "alternatebarcode"),
    "stock": ("stock", "openingstock", "qty", "quantity"),
    "reorder": ("reorder", "reorderlevel"),
    "material": ("material", "fabric"),
    "season": ("season",),
    "description": ("description", "details"),
    "rack": ("rack", "location", "racklocation", "shelf"),
}
REQUIRED = ("name", "size", "colour")
TEMPLATE_HEADERS = ["name", "size", "colour", "category", "brand", "hsn", "gst", "mrp", "price", "cost", "sku",
                    "barcode", "stock", "reorder", "material", "season", "description", "rack"]
# product-level columns: every row of one product must agree (a blank is fine)
PRODUCT_FIELDS = ("category", "brand", "hsn", "gst", "mrp", "cost", "material", "season", "description")


def _key(header) -> str:
    return re.sub(r"[\s_\-]+", "", str(header or "").strip().lower())


def _column_for(header):
    key = _key(header)
    for column, names in COLUMNS.items():
        if key in names:
            return column
    return None


# ---------------------------------------------------------------- reading the file

def read_rows(path):
    """The file as (headers, rows). `rows` is a list of (row number, {column: text}). Row 1 is the header.

    CSV (UTF-8 or the Windows/Excel code page) and Excel .xlsx are read. Raises ValueError with a message
    safe to show the user when the file cannot be used.
    """
    path = str(path)
    if not os.path.isfile(path):
        raise ValueError("The file was not found.")
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ValueError("The file is too big (the limit is 20 MB).")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        raw = _read_csv(path)
    elif ext in (".xlsx", ".xlsm"):
        raw = _read_xlsx(path)
    else:
        raise ValueError("Use a .csv or .xlsx file.")
    if not raw:
        raise ValueError("The file is empty.")
    headers = [str(h or "").strip() for h in raw[0]]
    columns = [_column_for(h) for h in headers]
    missing = [c for c in REQUIRED if c not in columns]
    if missing:
        raise ValueError("The file needs these columns: " + ", ".join(REQUIRED) + ". Missing: " + ", ".join(missing) + ".")
    seen = [c for c in columns if c]
    if len(seen) != len(set(seen)):
        raise ValueError("Two columns of the file mean the same thing. Keep one of them.")
    rows = []
    for number, cells in enumerate(raw[1:], start=2):
        values = {}
        for column, cell in zip(columns, cells):
            if column:
                values[column] = _text(cell)
        if any(values.values()):        # a fully blank line is ignored
            rows.append((number, values))
    if len(rows) > MAX_ROWS:
        raise ValueError(f"The file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
    return headers, rows


def _text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and cell == int(cell):
        return str(int(cell))            # Excel stores 8901234567890 as a number
    return str(cell).strip()


def _read_csv(path):
    data = open(path, "rb").read()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("The file's text could not be read. Save it as CSV UTF-8 and try again.")
    lines = text.splitlines()
    if not lines:
        return []
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(lines, dialect))


def _read_xlsx(path):
    import openpyxl

    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - a damaged or wrong file
        raise ValueError("The Excel file could not be opened.") from exc
    try:
        sheet = book.worksheets[0]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()


# ---------------------------------------------------------------- checking the rows

def _money(text, column, problems):
    if text == "":
        return None
    try:
        return parse_rupees(text)
    except ValueError as exc:
        problems.append((column, str(exc)))
        return None


def _whole(text, column, problems, *, limit):
    if text == "":
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        problems.append((column, "Enter a whole number."))
        return None
    if number != number.to_integral_value() or number < 0 or number > limit:
        problems.append((column, f"Enter a whole number from 0 to {limit:,}."))
        return None
    return int(number)


def _percent(text, problems):
    if text == "":
        return None
    try:
        number = Decimal(text.replace("%", "").strip())
    except InvalidOperation:
        problems.append(("gst", "GST must be a number, for example 5 or 12."))
        return None
    if number < 0 or number > 100:
        problems.append(("gst", "GST must be between 0 and 100."))
        return None
    return float(number)


def _check(conn, user_id, rows):
    """Check every row. Returns (items, problems). `items` are the clean rows; `problems` carry the row number."""
    can_cost = has_permission(conn, user_id, "view_cost")
    can_stock = has_permission(conn, user_id, "adjust_stock")
    problems_out = []
    items = []
    first_of_product = {}     # lower(name) -> the first item of that product in this file
    variant_seen = {}         # (lower name, lower size, lower colour) -> row number
    code_seen = {}            # upper(sku/barcode/alt) -> row number
    for number, raw in rows:
        found = []
        for column in REQUIRED:
            if not raw.get(column):
                found.append((column, "This is required."))
        for column, value in raw.items():
            if len(value) > MAX_TEXT:
                found.append((column, f"Too long (at most {MAX_TEXT} characters)."))
        item = {
            "row": number, "raw": raw,
            "name": raw.get("name", ""), "size": raw.get("size", ""), "colour": raw.get("colour", ""),
            "category": raw.get("category") or None, "brand": raw.get("brand") or None,
            "hsn": raw.get("hsn") or None, "material": raw.get("material") or None,
            "season": raw.get("season") or None, "description": raw.get("description") or None,
            "rack": raw.get("rack") or None,
            "gst": _percent(raw.get("gst", ""), found),
            "mrp": _money(raw.get("mrp", ""), "mrp", found),
            "price": _money(raw.get("price", ""), "price", found),
            "cost": _money(raw.get("cost", ""), "cost", found),
            "stock": _whole(raw.get("stock", ""), "stock", found, limit=MAX_STOCK),
            "reorder": _whole(raw.get("reorder", ""), "reorder", found, limit=MAX_STOCK),
            "sku": raw.get("sku") or None, "barcode": raw.get("barcode") or None,
            "alt_barcode": raw.get("alt_barcode") or None, "existing_product_id": None,
        }
        if item["cost"] is not None and not can_cost:
            found.append(("cost", "You are not allowed to set cost prices."))
        if item["stock"] and not can_stock:
            found.append(("stock", "You are not allowed to set opening stock."))
        for column in ("sku", "barcode", "alt_barcode"):
            code = item[column]
            if not code:
                continue
            if re.search(r"\s", code):
                found.append((column, "A code cannot contain spaces."))
                continue
            owner_row = code_seen.get(code.upper())
            if owner_row is not None:
                found.append((column, f"The same code is already used in row {owner_row}."))
                continue
            clash = conn.execute(
                "SELECT p.name, v.size, v.colour FROM variants v JOIN products p ON p.id = v.product_id "
                "WHERE upper(v.sku) = upper(?) OR upper(v.barcode) = upper(?) OR upper(v.alt_barcode) = upper(?) LIMIT 1",
                (code, code, code)).fetchone()
            if clash:
                found.append((column, f"This code is already used by {clash['name']} ({clash['size']}, {clash['colour']})."))
                continue
            code_seen[code.upper()] = number

        if item["name"] and item["size"] and item["colour"]:
            vkey = (item["name"].lower(), item["size"].lower(), item["colour"].lower())
            if vkey in variant_seen:
                found.append(("size", f"This size and colour of the product is already in row {variant_seen[vkey]}."))
            else:
                variant_seen[vkey] = number
            existing = conn.execute(
                "SELECT p.id FROM products p WHERE lower(p.name) = lower(?)", (item["name"],)).fetchone()
            item["existing_product_id"] = existing["id"] if existing else None
            if existing and conn.execute(
                    "SELECT 1 FROM variants WHERE product_id = ? AND lower(size) = lower(?) AND lower(colour) = lower(?)",
                    (existing["id"], item["size"], item["colour"])).fetchone():
                found.append(("size", "This size and colour already exists for this product. Nothing is overwritten."))
            first = first_of_product.setdefault(item["name"].lower(), item)
            if first is not item:
                for field in PRODUCT_FIELDS:
                    if item[field] is not None and first[field] is not None and item[field] != first[field]:
                        found.append((field, f"Differs from row {first['row']} of the same product."))
                    elif first[field] is None and item[field] is not None:
                        first[field] = item[field]      # fill a blank from a later row of the same product
        for column, message in found:
            problems_out.append({"row": number, "column": column, "message": message, "values": raw})
        item["ok"] = not found
        items.append(item)
    return items, problems_out


def preview_import(conn, user_id, path) -> dict:
    """Check a file and say what would happen. Saves nothing.

    Returns {'file_name', 'total_rows', 'new_products', 'added_to_existing', 'new_variants', 'opening_pieces',
    'problems': [{'row','column','message','values'}], 'headers', 'can_import'}.
    """
    require_permission(conn, user_id, "edit_products", "import products")
    headers, rows = read_rows(path)
    items, problems = _check(conn, user_id, rows)
    products, existing_products = set(), set()
    for item in items:
        if item["existing_product_id"]:
            existing_products.add(item["existing_product_id"])
        else:
            products.add(item["name"].lower())
    return {
        "file_name": os.path.basename(str(path)), "headers": headers, "total_rows": len(rows),
        "new_products": len(products), "added_to_existing": len(existing_products),
        "new_variants": len(items), "opening_pieces": sum(i["stock"] or 0 for i in items),
        "problems": problems, "bad_rows": len({p["row"] for p in problems}), "can_import": not problems and bool(items),
    }


def write_error_file(preview: dict, path) -> str:
    """Save the problems as a CSV: row, column, problem, then the original values. Returns the path."""
    path = str(path)
    columns = [c for c in TEMPLATE_HEADERS if any(c in p["values"] for p in preview["problems"])]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file row", "problem in column", "problem"] + columns)
        for problem in preview["problems"]:
            writer.writerow([problem["row"], problem["column"], problem["message"]]
                            + [problem["values"].get(c, "") for c in columns])
    return path


def write_template(path) -> str:
    """A small example file the owner can fill in."""
    path = str(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(TEMPLATE_HEADERS)
        writer.writerow(["Cotton Shirt", "M", "Blue", "Men", "Raymond", "6205", "5", "1999", "1050", "600", "", "", "10", "3",
                         "Cotton", "Summer", "", "A1"])
        writer.writerow(["Cotton Shirt", "L", "Blue", "Men", "Raymond", "6205", "5", "1999", "1050", "600", "", "", "8", "3",
                         "Cotton", "Summer", "", "A1"])
    return path


# ---------------------------------------------------------------- saving

def import_products(conn, user_id, path) -> dict:
    """Save the whole file, or nothing. Returns {'products', 'variants', 'added_to_existing', 'opening_pieces'}."""
    require_permission(conn, user_id, "edit_products", "import products")
    headers, rows = read_rows(path)
    with transaction(conn):
        items, problems = _check(conn, user_id, rows)       # checked again inside the same transaction
        if problems:
            raise ValueError(f"The file has {len({p['row'] for p in problems})} row(s) with problems. Nothing was imported.")
        if not items:
            raise ValueError("The file has no rows to import.")
        product_ids = {}
        made_products = 0
        for item in items:
            name_key = item["name"].lower()
            product_id = item["existing_product_id"] or product_ids.get(name_key)
            if product_id is None:
                first = next(i for i in items if i["name"].lower() == name_key)   # the details gathered from all its rows
                cur = conn.execute(
                    "INSERT INTO products(name, category, brand, hsn, material, season, description, gst_rate_percent, "
                    "mrp_paise, default_cost_paise, default_price_paise, unit, reorder_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (first["name"], first["category"], first["brand"], first["hsn"], first["material"], first["season"],
                     first["description"], first["gst"], first["mrp"], first["cost"] or 0, first["price"] or 0,
                     "piece", 0))
                product_id = cur.lastrowid
                made_products += 1
            product_ids[name_key] = product_id
            default_price = conn.execute("SELECT default_price_paise FROM products WHERE id = ?", (product_id,)).fetchone()[0]
            override = item["price"] if item["price"] is not None and item["price"] != default_price else None
            sku = item["sku"] or _sku(conn, product_id, item["size"], item["colour"])
            cur = conn.execute(
                "INSERT INTO variants(product_id, size, colour, sku, barcode, alt_barcode, price_override_paise, "
                "reorder_level, rack_location) VALUES (?,?,?,?,?,?,?,?,?)",
                (product_id, item["size"], item["colour"], sku, item["barcode"], item["alt_barcode"], override,
                 item["reorder"], item["rack"]))
            variant_id = cur.lastrowid
            if not item["barcode"]:
                conn.execute("UPDATE variants SET barcode = ? WHERE id = ?", (make_internal_barcode(variant_id), variant_id))
            if item["stock"]:
                add_movement(conn, variant_id, item["stock"], "opening stock (import)", user_id=user_id)
        result = {
            "products": made_products, "variants": len(items),
            "added_to_existing": len({i["existing_product_id"] for i in items if i["existing_product_id"]}),
            "opening_pieces": sum(i["stock"] or 0 for i in items),
        }
        log_event(conn, "products_imported", user_id=user_id, entity="import",
                  after={"file": os.path.basename(str(path)), **result})
    return result


def _sku(conn, product_id, size, colour) -> str:
    clean = lambda text: re.sub(r"[^A-Z0-9]", "", text.upper())  # noqa: E731
    base = f"P{product_id:04d}-{clean(size)}-{clean(colour)}"
    candidate, n = base, 2
    while conn.execute("SELECT 1 FROM variants WHERE upper(sku) = upper(?)", (candidate,)).fetchone():
        candidate = f"{base}-{n}"
        n += 1
    return candidate
