"""All the figures for the Dashboard (SRS section 5), gathered from the tested report functions.

Nothing here writes to the database and nothing does its own money arithmetic: every number comes
from core.reports_sales, core.reports_stock, core.reports_purchase, core.reports_money,
core.supplier_ledger, core.customer_credit or core.cash, so the dashboard always agrees with the
reports, the bills and the stock ledger, and no transaction is counted twice.

    figures = dashboard_figures(conn, user_id=uid, best_period="this_month")

returns a dict. A section the user may not see (no permission) is simply left out of the dict.
A section that failed for any other reason is named in figures["errors"]; the others still load.
"""

import logging
from datetime import date, timedelta

from core import cash, customer_credit, supplier_ledger
from core import reports_money, reports_purchase, reports_sales, reports_stock
from core.permissions import PermissionDenied
from core.report_common import resolve_period
from core.settings import get_bool

log = logging.getLogger(__name__)

BEST_PERIODS = ("today", "this_week", "this_month", "last_30_days")
LIST_SIZE = 5


def period_range(kind: str, today=None) -> tuple:
    """(from, to) for a best-seller period. 'last_30_days' is today and the 29 days before it."""
    today = today or date.today()
    if kind == "last_30_days":
        return (today - timedelta(days=29)).isoformat(), today.isoformat()
    if kind not in BEST_PERIODS:
        raise ValueError(f"Period must be one of: {', '.join(BEST_PERIODS)}.")
    return resolve_period(kind, today=today)


def best_sellers(conn, date_from, date_to, *, user_id, limit=LIST_SIZE) -> list:
    """Products sold in the period, most pieces first (pieces sold minus pieces returned)."""
    rows = []
    for r in reports_sales.sales_by(conn, date_from, date_to, "product", user_id=user_id):
        net_qty = (r["qty"] or 0) - (r["returned_qty"] or 0)
        if net_qty > 0:
            rows.append({"product": r["label"], "net_qty": net_qty, "net_taxable_paise": r["net_taxable_paise"]})
    rows.sort(key=lambda r: (-r["net_qty"], r["product"].lower()))
    return rows[:limit]


def products_without_sales(conn, date_from, date_to, *, user_id, limit=LIST_SIZE) -> dict:
    """Products that have sellable stock but no sale in the period, most stock first.

    `count` is how many such products there are; `rows` holds the first few. Products are matched by
    the name printed on the bill.
    """
    sold = {r["label"] for r in reports_sales.sales_by(conn, date_from, date_to, "product", user_id=user_id)
            if (r["qty"] or 0) > 0}
    in_stock = {}
    for item in reports_stock.current_stock(conn, user_id=user_id):
        if item["sellable_qty"] > 0:
            in_stock[item["product"]] = in_stock.get(item["product"], 0) + item["sellable_qty"]
    idle = [{"product": name, "qty": qty} for name, qty in in_stock.items() if name not in sold]
    idle.sort(key=lambda r: (-r["qty"], r["product"].lower()))
    return {"count": len(idle), "rows": idle[:limit]}


def _stock_block(conn, user_id) -> dict:
    items = reports_stock.current_stock(conn, user_id=user_id)
    block = {
        "total_qty": sum(i["sellable_qty"] for i in items),
        "value_paise": None, "method_label": None, "variants_without_cost": 0,
    }
    try:  # the value is cost information: only users who may see cost get it
        valuation = reports_stock.stock_valuation(conn, user_id=user_id)
    except PermissionDenied:
        return block
    block.update(
        value_paise=valuation["total_value_paise"], method_label=valuation["method_label"],
        variants_without_cost=valuation["variants_without_cost"],
    )
    return block


def _purchases_block(conn, day, user_id) -> dict:
    register = reports_purchase.purchase_register(conn, day, day, user_id=user_id)
    return {
        "count": register["purchase_count"], "qty": register["totals"]["qty"],
        "total_paise": register["totals"]["total_paise"],
    }


def _customer_dues_block(conn, today, user_id):
    """Total owed by customers. None when credit sales are switched off.

    Reads the open bills directly (not dues_report) because dues_report writes one customer-access-log
    line per customer, and a dashboard that refreshes must not fill that log.
    """
    if not get_bool(conn, "credit_sales_enabled"):
        return None
    customer_credit._require_read(conn, user_id)
    bills = customer_credit._open_bills(conn, as_of=today)
    return {
        "total_paise": customer_credit._ageing(bills)["total_paise"],
        "customers": len({b["customer_id"] for b in bills}),
        "overdue_paise": sum(b["due_paise"] for b in bills if b["overdue_days"] > 0),
    }


def _supplier_dues_block(conn, user_id) -> dict:
    owed = [b for b in supplier_ledger.all_supplier_balances(conn, user_id=user_id) if b["balance_paise"] > 0]
    return {"total_paise": sum(b["balance_paise"] for b in owed), "suppliers": len(owed)}


def _cash_block(conn, day, user_id) -> dict:
    today = cash.get_cash_day(conn, day, user_id=user_id)
    return {
        "status": today["status"], "expected_paise": today["expected_paise"],
        "open_day": cash.get_open_day(conn, user_id=user_id),
    }


def dashboard_figures(conn, *, user_id, today=None, best_period="this_month") -> dict:
    """Every dashboard figure for `today` (default: the real today), see the module text."""
    today = today or date.today()
    day = today.isoformat()
    best_from, best_to = period_range(best_period, today)
    out = {"date": day, "best_period": best_period, "best_from": best_from, "best_to": best_to, "errors": []}

    def section(name, fn):
        try:
            value = fn()
        except PermissionDenied:
            return
        except Exception:  # one broken figure must never take the whole dashboard down
            log.exception("Dashboard section '%s' failed", name)
            out["errors"].append(name)
            return
        if value is not None:
            out[name] = value

    section("sales", lambda: reports_sales.sales_summary(conn, day, day, user_id=user_id))
    section("payments", lambda: reports_sales.payment_mode_summary(conn, day, day, user_id=user_id))
    section("purchases", lambda: _purchases_block(conn, day, user_id))
    section("stock", lambda: _stock_block(conn, user_id))
    section("low_stock", lambda: reports_stock.low_stock(conn, user_id=user_id))
    section("best_sellers", lambda: best_sellers(conn, best_from, best_to, user_id=user_id))
    section("no_sales", lambda: products_without_sales(conn, best_from, best_to, user_id=user_id))
    section("expenses", lambda: reports_money.expense_report(conn, day, day, user_id=user_id)["totals"]["net_paise"])
    section("cash", lambda: _cash_block(conn, day, user_id))
    section("customer_dues", lambda: _customer_dues_block(conn, today, user_id))
    section("supplier_dues", lambda: _supplier_dues_block(conn, user_id))
    return out