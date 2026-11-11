"""STK-07 / STK-08: how long stock has been lying on the shelf, and which products to mark down.

Age of a variant's stock = days since it was last RECEIVED (the latest purchase); when it never came by a
purchase (opening stock, import, count), days since its first stock-in. Sales, returns and counts do not
make old stock "new". Only stock you can sell today (the sellable bucket) is counted.

  stock_ageing(conn, user_id=, on_date=, season=, min_age_days=)   rows + totals per age band
  aged_product_ids(conn, user_id, min_age_days, season=, on_date=) products whose ALL sellable stock is that old,
                                                                    ready to be marked down in Bulk Edit
Markdown itself is a bulk price change (core.product_edit.bulk_edit_products): preview first, one transaction,
audit log, never touches saved bills.
"""

from datetime import date

from core.permissions import has_permission
from core.report_common import iso_date, require_view

BANDS = ((30, "0-30 days"), (60, "31-60 days"), (90, "61-90 days"), (180, "91-180 days"), (None, "Over 180 days"))
NOTE = ("Age is counted from the last purchase of that item (or its first stock-in when it never came by purchase). "
        "Only stock you can sell today is listed.")


def band_of(days: int) -> str:
    for limit, text in BANDS:
        if limit is None or days <= limit:
            return text
    return BANDS[-1][1]


def _ages(conn, on: date) -> dict:
    """{variant_id: (first_in_day, last_purchase_day)} for variants that ever had stock in."""
    return {r["variant_id"]: (r["first_day"], r["last_purchase"]) for r in conn.execute(
        "SELECT variant_id, MIN(substr(created_at,1,10)) AS first_day, "
        "MAX(CASE WHEN doc_type = 'purchase' THEN substr(created_at,1,10) END) AS last_purchase "
        "FROM stock_movements WHERE bucket = 'sellable' AND qty_change > 0 AND substr(created_at,1,10) <= ? "
        "GROUP BY variant_id", (on.isoformat(),))}


def _rows(conn, on, season):
    sql = ("SELECT p.id AS product_id, p.name AS product, p.category, p.season, v.id AS variant_id, v.size, v.colour, "
           "s.sellable_qty FROM variants v JOIN products p ON p.id = v.product_id "
           "JOIN variant_stock s ON s.variant_id = v.id "
           "WHERE v.is_active = 1 AND p.is_active = 1 AND s.sellable_qty > 0")
    args = []
    if season:
        sql += " AND lower(p.season) = lower(?)"
        args.append(season.strip())
    ages = _ages(conn, on)
    out = []
    for r in conn.execute(sql, args):
        got = ages.get(r["variant_id"])
        if not got:
            continue
        since = got[1] or got[0]
        row = {k: r[k] for k in r.keys()}
        row["received"] = since
        row["age_days"] = max(0, (on - date.fromisoformat(since)).days)
        row["band"] = band_of(row["age_days"])
        out.append(row)
    return out


def _date(on_date):
    return date.fromisoformat(iso_date(on_date, "Date")) if on_date else date.today()


def stock_ageing(conn, *, user_id, on_date=None, season=None, min_age_days=0) -> dict:
    require_view(conn, user_id)
    if isinstance(min_age_days, bool) or not isinstance(min_age_days, int) or min_age_days < 0:
        raise ValueError("The number of days must be zero or more.")
    on = _date(on_date)
    show_cost = has_permission(conn, user_id, "view_cost")
    from core.reports_stock import _last_purchase
    rows = [r for r in _rows(conn, on, season) if r["age_days"] >= min_age_days]
    for r in rows:
        if show_cost:
            bought = _last_purchase(conn, r["variant_id"])
            r["stock_value_paise"] = r["sellable_qty"] * bought["unit_cost_paise"] if bought else None
    rows.sort(key=lambda r: (-r["age_days"], r["product"].lower(), (r["colour"] or "").lower(), r["size"] or ""))
    bands = []
    for _limit, text in BANDS:
        in_band = [r for r in rows if r["band"] == text]
        item = {"band": text, "variants": len(in_band), "pieces": sum(r["sellable_qty"] for r in in_band)}
        if show_cost:
            item["stock_value_paise"] = sum(r.get("stock_value_paise") or 0 for r in in_band)
        bands.append(item)
    return {"on_date": on.isoformat(), "rows": rows, "bands": bands, "units_in_stock": sum(r["sellable_qty"] for r in rows),
            "note": NOTE}


def aged_product_ids(conn, user_id, min_age_days, *, season=None, on_date=None) -> list:
    """Products whose every sellable piece is at least `min_age_days` old (candidates for a markdown)."""
    require_view(conn, user_id)
    if isinstance(min_age_days, bool) or not isinstance(min_age_days, int) or min_age_days < 0:
        raise ValueError("The number of days must be zero or more.")
    youngest = {}
    for r in _rows(conn, _date(on_date), season):
        youngest[r["product_id"]] = min(youngest.get(r["product_id"], r["age_days"]), r["age_days"])
    return sorted(pid for pid, age in youngest.items() if age >= min_age_days)
