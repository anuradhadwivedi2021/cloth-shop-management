# db/migration_15.py
"""Schema migration 15: make a scan by SKU or supplier barcode fast on a big shop (SRS: scan to line within 0.3 s).

The lookup compares upper(sku), which the plain SKU index cannot serve, so every scan read the whole variants
table. An index on the same expression fixes that. Nothing else changes.
"""

MIGRATION_15 = """
CREATE INDEX idx_variants_sku_upper ON variants(upper(sku));
"""
