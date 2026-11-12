# db/migration_23.py
"""Schema migration 23: fabric sold by the metre (SRS PRD-11).

A product whose unit is 'metre' keeps its stock and its bill quantities in whole CENTIMETRES (so 2.5 m is 250), and its
selling price is per metre in whole rupees. Nothing else in the money or stock code had to change.

products.min_cut_cm   the shortest length the shop will cut (0 = no minimum)
fabric_pieces         the rolls and remnants (cut-offs) on the shelf: how long each was and how much is left.
                      A sale takes from remnants first, then the oldest roll. The stock total still comes from stock
                      movements only; this table says which pieces it is made of.
"""

MIGRATION_23 = """
ALTER TABLE products ADD COLUMN min_cut_cm INTEGER NOT NULL DEFAULT 0 CHECK (min_cut_cm >= 0);
CREATE TABLE fabric_pieces (
    id           INTEGER PRIMARY KEY,
    variant_id   INTEGER NOT NULL REFERENCES variants(id),
    kind         TEXT NOT NULL CHECK (kind IN ('roll', 'remnant')),
    ref          TEXT,
    length_cm    INTEGER NOT NULL CHECK (length_cm > 0),
    remaining_cm INTEGER NOT NULL CHECK (remaining_cm >= 0 AND remaining_cm <= length_cm),
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX idx_fabric_pieces_variant ON fabric_pieces(variant_id, remaining_cm);
CREATE TRIGGER fabric_pieces_no_delete BEFORE DELETE ON fabric_pieces
BEGIN SELECT RAISE(ABORT, 'a fabric piece cannot be deleted'); END;
"""
