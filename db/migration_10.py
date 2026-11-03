# db/migration_10.py
"""Schema migration 10 (Step 1 of Phase A): Decimal Qty (metre) and fabric cut length integration.

1. Updates quantities in stock_movements, sale_lines, and purchase_lines to milli-units (x1000)
   to support exact 3-decimal precision for fabric sold by metre without floating-point errors.
2. Adds min_cut_milli column to variants for minimum fabric cut length validation.
3. Adds fractional quantity validation trigger to enforce integer milli-units (multiples of 1000)
   for non-metre items (piece, pair, set).
"""

MIGRATION_10 = """
-- 1. Add minimum cut length in milli-units to variants (default 0)
ALTER TABLE variants ADD COLUMN min_cut_milli INTEGER NOT NULL DEFAULT 0 CHECK (min_cut_milli >= 0);

-- 2. Convert existing integer quantities to milli-units (1 unit = 1000 milli-units)
UPDATE stock_movements SET qty_change = qty_change * 1000, balance_after = balance_after * 1000;
UPDATE sale_lines SET qty = qty * 1000;
UPDATE purchase_lines SET qty = qty * 1000;
UPDATE sale_return_lines SET qty = qty * 1000;

-- 3. Validation Trigger: Block fractional quantities for piece, pair, and set
CREATE TRIGGER sale_lines_check_unit_fraction BEFORE INSERT ON sale_lines
FOR EACH ROW
WHEN (SELECT unit FROM products WHERE id = (SELECT product_id FROM variants WHERE id = NEW.variant_id)) IN ('piece', 'pair', 'set')
  AND (NEW.qty % 1000 != 0)
BEGIN
    SELECT RAISE(ABORT, 'Fractional quantities are only allowed for fabric items sold by metre');
END;
"""