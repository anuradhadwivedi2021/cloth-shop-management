"""Save a report as a CSV file (Excel opens it). Uses only Python's built-in csv module.

Rules:
  * Needs the 'export_reports' permission, checked here and not only on the screen.
  * Amounts stored as paise are written as rupees: a column 'total_paise' becomes 'total (Rs)'
    and 123450 becomes '1234.50' (whole-number arithmetic, never float).
  * A text cell that starts with = + - @ is written with a leading ' so Excel never runs it as a formula.
  * The file appears under its real name only when it is complete, and the export is written to the
    audit log in the same transaction. If either fails, no half file is left behind.
  * Report rows may hold personal data (customer names, phone numbers): hand such files only to people allowed to see them.
"""

import csv
import json
import os

from core.audit import log_event
from core.permissions import require_permission
from db.connection import transaction

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _rupees(paise) -> str:
    sign = "-" if paise < 0 else ""
    whole, part = divmod(abs(paise), 100)
    return f"{sign}{whole}.{part:02d}"


def _header(name: str) -> str:
    return f"{name[:-6]} (Rs)" if name.endswith("_paise") else name


def _cell(column: str, value):
    if value is None:
        return ""
    if column.endswith("_paise") and isinstance(value, int) and not isinstance(value, bool):
        return _rupees(value)
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def export_report_csv(conn, path, report, *, user_id, report_name, columns=None) -> str:
    """Write a report to `path`. `report` is a list of row dicts or a report dict with a 'rows' list.

    `columns` chooses and orders the columns (default: the keys of the first row).
    Returns the path.
    """
    require_permission(conn, user_id, "export_reports", "export reports")
    # most reports keep their lines under "rows"; the day book keeps them under "entries"
    rows = (report.get("rows") if "rows" in report else report.get("entries")) if isinstance(report, dict) else report
    if not isinstance(rows, (list, tuple)):
        raise ValueError("There is nothing to export for this report.")
    columns = list(columns) if columns else (list(rows[0].keys()) if rows else [])
    if not columns:
        raise ValueError("There is nothing to export for this report.")

    path = os.fspath(path)
    temp = f"{path}.part"
    try:
        with open(temp, "w", encoding="utf-8-sig", newline="") as handle:  # utf-8-sig: Excel shows Hindi/Marathi right
            writer = csv.writer(handle)
            writer.writerow([_header(c) for c in columns])
            for row in rows:
                writer.writerow([_cell(c, row.get(c)) for c in columns])
        with transaction(conn):
            log_event(conn, "report_exported", user_id=user_id, entity="report", entity_id=None,
                      after={"report": report_name, "rows": len(rows), "file": os.path.basename(path)})
            os.replace(temp, path)
    except Exception:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    return path