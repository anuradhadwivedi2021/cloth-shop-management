"""BIL-14: who sold, and what commission each salesperson has earned.

Commission is a percentage of NET SALES (before tax): the taxable value of the person's bills minus the taxable value
of the credit notes made against those bills, in the chosen dates. A return therefore takes commission back.
It is a report only: the app does not pay anyone. Off by default (Settings > Commission).

  list_salespeople(conn, user_id)                       active people a bill can be credited to, with their rate
  set_rate(conn, admin_id, person_id, percent)          a person's own rate; None goes back to the shop default
  commission_report(conn, date_from, date_to, user_id=) one row per person + totals + the formula
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from core.audit import log_event
from core.permissions import require_permission
from core.report_common import check_range, require_view
from core.reports_sales import _in
from core.settings import get_bool, get_setting
from db.connection import transaction

FORMULA = ("Commission = net sales x rate. Net sales = taxable value of the person's bills minus the taxable value "
           "of credit notes made against those bills, before tax.")
MAX_PERCENT = 50


def enabled(conn) -> bool:
    return get_bool(conn, "commission_enabled")


def default_bp(conn) -> int:
    try:
        return int(get_setting(conn, "commission_default_bp", 0))
    except (TypeError, ValueError):
        return 0


def percent_text(bp) -> str:
    return f"{bp / 100:.2f}".rstrip("0").rstrip(".") if bp % 100 else str(bp // 100)


def parse_percent(text) -> int:
    """'1.5' -> 150 basis points. Whole and half-hundredths of a percent only."""
    try:
        value = Decimal(str(text).strip().rstrip("%"))
    except (InvalidOperation, ValueError):
        raise ValueError("The commission must be a number, like 1.5") from None
    if value < 0 or value > MAX_PERCENT:
        raise ValueError(f"The commission must be between 0 and {MAX_PERCENT} percent.")
    bp = value * 100
    if bp != bp.to_integral_value():
        raise ValueError("The commission can have at most 2 decimal places.")
    return int(bp)


def list_salespeople(conn, user_id) -> list:
    """Active people with their commission rate (own rate if set, else the shop default)."""
    require_permission(conn, user_id, "sell", "see salespeople")
    default = default_bp(conn)
    rows = conn.execute(
        "SELECT u.id, COALESCE(NULLIF(u.full_name,''), u.username) AS name, r.rate_bp FROM users u "
        "LEFT JOIN commission_rates r ON r.user_id = u.id WHERE u.is_active = 1 ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id": r["id"], "name": r["name"], "own_rate_bp": r["rate_bp"],
             "rate_bp": default if r["rate_bp"] is None else r["rate_bp"]} for r in rows]


def set_rate(conn, admin_id, person_id, percent) -> None:
    """Set a person's own rate (percent as text or number); percent=None removes it (shop default applies)."""
    require_permission(conn, admin_id, "manage_settings", "change commission")
    bp = None if percent is None or str(percent).strip() == "" else parse_percent(percent)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (person_id,)).fetchone() is None:
            raise ValueError("User not found.")
        before = conn.execute("SELECT rate_bp FROM commission_rates WHERE user_id = ?", (person_id,)).fetchone()
        if bp is None:
            conn.execute("DELETE FROM commission_rates WHERE user_id = ?", (person_id,))
        else:
            conn.execute("INSERT INTO commission_rates(user_id, rate_bp, changed_by) VALUES (?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET rate_bp = excluded.rate_bp, changed_by = excluded.changed_by, "
                         "changed_at = strftime('%Y-%m-%d %H:%M:%S','now','localtime')", (person_id, bp, admin_id))
        log_event(conn, "commission_rate_changed", user_id=admin_id, entity="user", entity_id=person_id,
                  before={"rate_bp": before["rate_bp"] if before else None}, after={"rate_bp": bp})


def _commission(net_paise, bp) -> int:
    """Half-up in paise, the same for gains and clawbacks."""
    value = (Decimal(net_paise) * bp / Decimal(10000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(value)


def commission_report(conn, date_from, date_to, *, user_id) -> dict:
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    default = default_bp(conn)
    sold = {r["sid"]: r for r in conn.execute(
        "SELECT salesperson_id AS sid, COUNT(*) AS bills, COALESCE(SUM(taxable_paise),0) AS taxable FROM sales "
        f"WHERE status = 'completed' AND {_in('created_at')} GROUP BY salesperson_id", (start, end))}
    returned = {r["sid"]: r["taxable"] for r in conn.execute(
        "SELECT s.salesperson_id AS sid, COALESCE(SUM(r.taxable_paise),0) AS taxable FROM sale_returns r "
        f"JOIN sales s ON s.id = r.sale_id WHERE {_in('r.created_at')} GROUP BY s.salesperson_id", (start, end))}
    names = {r["id"]: (r["name"], r["rate_bp"]) for r in conn.execute(
        "SELECT u.id, COALESCE(NULLIF(u.full_name,''), u.username) AS name, r.rate_bp FROM users u "
        "LEFT JOIN commission_rates r ON r.user_id = u.id")}
    rows = []
    for sid in sorted(set(sold) | set(returned), key=lambda s: names.get(s, ("", 0))[0].lower()):
        name, own = names.get(sid, (f"user {sid}", None))
        bp = default if own is None else own
        sales = sold.get(sid)
        net = (sales["taxable"] if sales else 0) - returned.get(sid, 0)
        rows.append({"person_id": sid, "person": name, "bills": sales["bills"] if sales else 0,
                     "sales_taxable_paise": sales["taxable"] if sales else 0, "returns_taxable_paise": returned.get(sid, 0),
                     "net_sales_paise": net, "rate_bp": bp, "rate_percent": percent_text(bp),
                     "commission_paise": _commission(net, bp)})
    return {"date_from": start, "date_to": end, "enabled": enabled(conn), "rows": rows, "formula": FORMULA,
            "total_net_sales_paise": sum(r["net_sales_paise"] for r in rows),
            "total_commission_paise": sum(r["commission_paise"] for r in rows)}
