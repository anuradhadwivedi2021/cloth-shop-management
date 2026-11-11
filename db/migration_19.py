# db/migration_19.py
"""Schema migration 19: financial-year lock (SRS USR-05).

locked_years   a financial year (e.g. '25-26') the owner has closed. While it is locked, no bill, credit note,
               confirmed purchase or expense dated in that year can be added. The core checks first (a clear message);
               these triggers are the safety net if anything ever skips the check.
"""

_FY_OF = ("(CASE WHEN CAST(substr({d},6,2) AS INTEGER) >= 4 "
          "THEN substr({d},3,2) || '-' || printf('%02d', (CAST(substr({d},3,2) AS INTEGER) + 1) % 100) "
          "ELSE printf('%02d', (CAST(substr({d},3,2) AS INTEGER) + 99) % 100) || '-' || substr({d},3,2) END)")

MIGRATION_19 = f"""
CREATE TABLE locked_years (
    fin_year   TEXT PRIMARY KEY,
    locked_by  INTEGER NOT NULL REFERENCES users(id),
    locked_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

CREATE TRIGGER sales_fy_locked BEFORE INSERT ON sales
WHEN EXISTS (SELECT 1 FROM locked_years WHERE fin_year = NEW.fin_year)
BEGIN SELECT RAISE(ABORT, 'financial year is locked'); END;

CREATE TRIGGER sale_returns_fy_locked BEFORE INSERT ON sale_returns
WHEN EXISTS (SELECT 1 FROM locked_years WHERE fin_year = NEW.fin_year)
BEGIN SELECT RAISE(ABORT, 'financial year is locked'); END;

CREATE TRIGGER purchases_fy_locked_insert BEFORE INSERT ON purchases
WHEN NEW.status = 'confirmed' AND EXISTS (SELECT 1 FROM locked_years WHERE fin_year = NEW.fin_year)
BEGIN SELECT RAISE(ABORT, 'financial year is locked'); END;

CREATE TRIGGER purchases_fy_locked_confirm BEFORE UPDATE OF status ON purchases
WHEN NEW.status = 'confirmed' AND OLD.status <> 'confirmed'
 AND EXISTS (SELECT 1 FROM locked_years WHERE fin_year = NEW.fin_year)
BEGIN SELECT RAISE(ABORT, 'financial year is locked'); END;

CREATE TRIGGER expenses_fy_locked BEFORE INSERT ON expenses
WHEN EXISTS (SELECT 1 FROM locked_years WHERE fin_year = {_FY_OF.format(d='NEW.expense_date')})
BEGIN SELECT RAISE(ABORT, 'financial year is locked'); END;
"""
