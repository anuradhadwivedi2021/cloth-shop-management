"""PRD-07: pictures of a product (PNG or JPEG, at most 2 MB each, at most 4 per product).

The picture is stored inside the encrypted shop database, so backups carry it and nothing is left in a plain folder.
The file is read and checked here: its real content must be PNG or JPEG (the file name is not trusted).

  add_image(conn, user_id, product_id, path)      needs 'edit_products'
  list_images(conn, user_id, product_id)          [{'id', 'file_name', 'mime', 'size_bytes'}]  (needs 'view_products')
  get_image(conn, user_id, image_id)              {'mime', 'data', 'file_name'}
  remove_image(conn, user_id, image_id)           needs 'edit_products'
"""

import os

from core.audit import log_event
from core.permissions import require_permission
from db.connection import transaction

MAX_BYTES = 2 * 1024 * 1024
MAX_PER_PRODUCT = 4


def _mime_of(data: bytes):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def _product(conn, product_id):
    if conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone() is None:
        raise ValueError("Product not found.")


def add_image(conn, user_id, product_id, path) -> int:
    require_permission(conn, user_id, "edit_products", "add product pictures")
    path = os.fspath(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        raise ValueError("The picture file could not be read.") from None
    if size > MAX_BYTES:
        raise ValueError("A picture can be at most 2 MB.")
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        raise ValueError("The picture file could not be read.") from None
    if not data or len(data) > MAX_BYTES:
        raise ValueError("A picture can be at most 2 MB." if data else "The picture file is empty.")
    mime = _mime_of(data)
    if mime is None:
        raise ValueError("Only PNG or JPEG pictures can be added.")
    name = os.path.basename(path)[:120] or "picture"
    with transaction(conn):
        _product(conn, product_id)
        if conn.execute("SELECT COUNT(*) FROM product_images WHERE product_id = ?", (product_id,)).fetchone()[0] >= MAX_PER_PRODUCT:
            raise ValueError(f"A product can have at most {MAX_PER_PRODUCT} pictures. Remove one first.")
        image_id = conn.execute(
            "INSERT INTO product_images(product_id, file_name, mime, size_bytes, data, created_by) VALUES (?,?,?,?,?,?)",
            (product_id, name, mime, len(data), data, user_id)).lastrowid
        log_event(conn, "product_image_added", user_id=user_id, entity="product", entity_id=product_id,
                  after={"image_id": image_id, "size_bytes": len(data)})
    return image_id


def list_images(conn, user_id, product_id) -> list:
    require_permission(conn, user_id, "view_products", "see product pictures")
    return [dict(r) for r in conn.execute(
        "SELECT id, file_name, mime, size_bytes FROM product_images WHERE product_id = ? ORDER BY id", (product_id,))]


def get_image(conn, user_id, image_id) -> dict:
    require_permission(conn, user_id, "view_products", "see product pictures")
    row = conn.execute("SELECT file_name, mime, data FROM product_images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise ValueError("Picture not found.")
    return {"file_name": row["file_name"], "mime": row["mime"], "data": bytes(row["data"])}


def remove_image(conn, user_id, image_id) -> None:
    require_permission(conn, user_id, "edit_products", "remove product pictures")
    with transaction(conn):
        row = conn.execute("SELECT product_id FROM product_images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise ValueError("Picture not found.")
        conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
        log_event(conn, "product_image_removed", user_id=user_id, entity="product", entity_id=row["product_id"],
                  after={"image_id": image_id})
