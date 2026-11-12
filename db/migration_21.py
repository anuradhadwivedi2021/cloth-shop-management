# db/migration_21.py
"""Schema migration 21: wedding anniversary on a customer (SRS CUS-07, with the birthday call list)."""

MIGRATION_21 = """
ALTER TABLE customers ADD COLUMN anniversary TEXT;
"""
