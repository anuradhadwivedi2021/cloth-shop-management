"""Physical stock count.

Flow:
  1. start_count    freezes the expected (system) quantity of each variant in scope.
  2. record_count   staff enter what they actually counted (editable until applied).
  3. variance_report shows expected / counted / difference for the owner.
  4. apply_count    owner approves; each difference becomes ONE stock movement.
     cancel_count   closes the count without touching stock.

Rules:
  * Blind count: the sheet hides the expected quantity while the count is open.
  * Every difference needs a reason before it can be applied.
  * If a variant's stock changed after the count started (a sale or purchase),
    applying is refused for that count, because the difference would be wrong.
    Cancel it and start a new count.
  * Expected and counted quantities are kept forever (before-and-after record).
  * Only sellable stock is counted.
"""

from datetime import date

from core.audit import log_event
from core.numbering import financial_year, next_number
from core.roles import require_role
from core.stock import add_movement, get_stock
from db.connection import transaction

COUNT_SERIES = "CNT"
COUNT_ROLES = ("owner", "stock")


def _open_count(conn, count_id):
    count = conn.execute("SELECT * FROM stock_counts WHERE id = ?", (count_id,)).fetchone()
    if count is None:
        raise ValueError("Stock count not found.")
    if count["status"] != "open":
        raise ValueError(f"This stock count is already {count['status']}.")
    return count


def start_count(conn, *, user_id, variant_ids=None, category=None, rack=None, blind=False, note=None) -> int:
    """Start a count of the chosen variants (all active variants if no filter). Returns the count id."""
    with transaction(conn):
        require_role(conn, user_id, COUNT_ROLES, "start a stock count")
        sql = (
            "SELECT v.id FROM variants v JOIN products p ON p.id = v.product_id "
            "WHERE v.is_active = 1"
        )
        args, scope = [], []
        if variant_ids is not None:
            if not variant_ids:
                raise ValueError("Select at least one variant to count.")
            sql += " AND v.id IN (" + ",".join("?" * len(variant_ids)) + ")"
            args += list(variant_ids)
            scope.append(f"{len(variant_ids)} selected variants")
        if category:
            sql += " AND lower(p.category) = lower(?)"
            args.append(category.strip())
            scope.append(f"category {category.strip()}")
        if rack:
            sql += " AND v.rack_location LIKE ?"
            args.append(rack.strip() + "%")
            scope.append(f"rack {rack.strip()}")
        ids = [r["id"] for r in conn.execute(sql + " ORDER BY v.id", args)]
        if not ids:
            raise ValueError("No active variants match this count.")

        _, count_no = next_number(conn, COUNT_SERIES, financial_year(date.today()), COUNT_SERIES)
        cur = conn.execute(
            "INSERT INTO stock_counts(count_no, fin_year, blind, scope, note, created_by) VALUES (?,?,?,?,?,?)",
            (count_no, financial_year(date.today()), 1 if blind else 0,
             ", ".join(scope) or "full shop", note, user_id),
        )
        count_id = cur.lastrowid
        for vid in ids:
            conn.execute(
                "INSERT INTO stock_count_lines(count_id, variant_id, expected_qty) VALUES (?,?,?)",
                (count_id, vid, get_stock(conn, vid)),
            )
        log_event(
            conn, "stock_count_started", user_id=user_id, entity="stock_count", entity_id=count_id,
            after={"count_no": count_no, "variants": len(ids), "blind": bool(blind)},
        )
        return count_id


def record_count(conn, count_id, variant_id, counted_qty, *, user_id, reason=None) -> None:
    """Enter (or correct) the counted quantity of one variant while the count is open."""
    with transaction(conn):
        require_role(conn, user_id, COUNT_ROLES, "enter stock counts")
        _open_count(conn, count_id)
        if not isinstance(counted_qty, int) or counted_qty < 0:
            raise ValueError("Counted quantity must be a whole number, zero or more.")
        cur = conn.execute(
            "UPDATE stock_count_lines SET counted_qty = ?, reason = ?, counted_by = ?, "
            "counted_at = datetime('now','localtime') WHERE count_id = ? AND variant_id = ?",
            (counted_qty, (reason or "").strip() or None, user_id, count_id, variant_id),
        )
        if cur.rowcount == 0:
            raise ValueError("This variant is not part of the stock count.")


def get_count_sheet(conn, count_id, *, reveal: bool = False) -> dict:
    """The count sheet. A blind count hides expected quantities while it is open (unless reveal=True)."""
    count = conn.execute("SELECT * FROM stock_counts WHERE id = ?", (count_id,)).fetchone()
    if count is None:
        raise ValueError("Stock count not found.")
    hide = bool(count["blind"]) and count["status"] == "open" and not reveal
    lines = []
    for r in conn.execute(
        "SELECT l.*, p.name AS product_name, v.size, v.colour, v.sku, v.barcode, v.rack_location "
        "FROM stock_count_lines l JOIN variants v ON v.id = l.variant_id "
        "JOIN products p ON p.id = v.product_id WHERE l.count_id = ? ORDER BY l.id",
        (count_id,),
    ):
        line = dict(r)
        if hide:
            line["expected_qty"] = None
        line["difference"] = (
            None if hide or r["counted_qty"] is None else r["counted_qty"] - r["expected_qty"]
        )
        lines.append(line)
    return {"count": dict(count), "lines": lines}


def variance_report(conn, count_id) -> list:
    """Counted lines whose quantity differs from the system quantity (for the owner to review)."""
    sheet = get_count_sheet(conn, count_id, reveal=True)
    return [l for l in sheet["lines"] if l["difference"]]


def apply_count(conn, count_id, *, approver_id) -> dict:
    """Owner approval: turn each difference into a stock movement. Returns a short summary."""
    with transaction(conn):
        require_role(conn, approver_id, ("owner",), "approve a stock count")
        count = _open_count(conn, count_id)
        lines = conn.execute(
            "SELECT * FROM stock_count_lines WHERE count_id = ? AND counted_qty IS NOT NULL ORDER BY id",
            (count_id,),
        ).fetchall()
        if not lines:
            raise ValueError("Nothing has been counted yet.")

        stale = [l["variant_id"] for l in lines if get_stock(conn, l["variant_id"]) != l["expected_qty"]]
        if stale:
            raise ValueError(
                "Stock changed after this count started for variant(s) "
                + ", ".join(str(v) for v in stale)
                + ". Cancel this count and start a new one."
            )
        missing_reason = [
            l["variant_id"] for l in lines
            if l["counted_qty"] != l["expected_qty"] and not (l["reason"] or "").strip()
        ]
        if missing_reason:
            raise ValueError(
                "A reason is required for every difference. Missing for variant(s) "
                + ", ".join(str(v) for v in missing_reason) + "."
            )

        added = removed = adjusted = 0
        changes = []
        for l in lines:
            diff = l["counted_qty"] - l["expected_qty"]
            if diff == 0:
                continue
            add_movement(
                conn, l["variant_id"], diff, f"stock count {count['count_no']}: {l['reason'].strip()}",
                doc_type="stock_count", doc_id=count_id, user_id=approver_id,
            )
            adjusted += 1
            added += max(diff, 0)
            removed += max(-diff, 0)
            changes.append({
                "variant_id": l["variant_id"], "before": l["expected_qty"],
                "after": l["counted_qty"], "reason": l["reason"].strip(),
            })

        conn.execute(
            "UPDATE stock_counts SET status = 'applied', approved_by = ?, closed_at = datetime('now','localtime') "
            "WHERE id = ?",
            (approver_id, count_id),
        )
        log_event(
            conn, "stock_count_applied", user_id=approver_id, entity="stock_count", entity_id=count_id,
            after={"count_no": count["count_no"], "changes": changes},
        )
        return {"variants_adjusted": adjusted, "units_added": added, "units_removed": removed}


def cancel_count(conn, count_id, *, user_id, reason) -> None:
    """Close an open count without changing any stock."""
    if not (reason or "").strip():
        raise ValueError("A reason is required to cancel a stock count.")
    with transaction(conn):
        require_role(conn, user_id, COUNT_ROLES, "cancel a stock count")
        count = _open_count(conn, count_id)
        conn.execute(
            "UPDATE stock_counts SET status = 'cancelled', closed_at = datetime('now','localtime'), "
            "close_reason = ? WHERE id = ?",
            (reason.strip(), count_id),
        )
        log_event(
            conn, "stock_count_cancelled", user_id=user_id, entity="stock_count", entity_id=count_id,
            after={"count_no": count["count_no"], "reason": reason.strip()},
        )
