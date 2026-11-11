# db/migration_18.py
"""Schema migration 18: product pictures (SRS PRD-07).

product_images   the picture itself is kept INSIDE the encrypted shop database (so every backup carries it and it
                 is never left lying in a plain folder). At most 4 per product and 2 MB each; the core checks that.
"""

MIGRATION_18 = """
CREATE TABLE product_images (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    file_name   TEXT NOT NULL,
    mime        TEXT NOT NULL CHECK (mime IN ('image/png', 'image/jpeg')),
    size_bytes  INTEGER NOT NULL CHECK (size_bytes > 0),
    data        BLOB NOT NULL,
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX idx_product_images_product ON product_images(product_id);
CREATE TRIGGER product_images_no_update BEFORE UPDATE ON product_images
BEGIN SELECT RAISE(ABORT, 'a product picture cannot be edited; remove it and add another'); END;
"""
