"""Analytics reports that sit on top of the stock ledger.

  sell_through(conn, date_from, date_to, user_id=)   per product: how much of what you had in the period was sold

Sell-through % = pieces sold / (pieces sold + stock left at the end of the period) x 100.
A product that sold everything it had is 100%. Products with no sales and no stock are not listed.
"""

from decimal import Decimal, ROUND_HALF_UP

from core.reports_stock import stock_flow

FORMULA = ("Sell-through % = pieces sold / (pieces sold + stock left at the end of the period) x 100. "
           "Sorted from the best seller to the slowest.")


def _pct(sold, available) -> str:
    if available <= 0:
        return "0"
    return str((Decimal(sold) * 100 / Decimal(available)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def sell_through(conn, date_from, date_to, *, user_id) -> dict:
    flow = stock_flow(conn, date_from, date_to, user_id=user_id)
    by_product = {}
    for r in flow["rows"]:
        p = by_product.setdefault(r["product"], {"product": r["product"], "sold": 0, "closing": 0})
        p["sold"] += r["sold"]
        p["closing"] += r["closing"]
    rows = []
    for p in by_product.values():
        p["available"] = p["sold"] + max(p["closing"], 0)
        if p["available"] <= 0:
            continue
        p["sell_through_percent"] = _pct(p["sold"], p["available"])
        rows.append(p)
    rows.sort(key=lambda x: (-Decimal(x["sell_through_percent"]), x["product"].lower()))
    return {"date_from": flow["date_from"], "date_to": flow["date_to"], "rows": rows, "formula": FORMULA,
            "total_available": sum(r["available"] for r in rows), "total_sold": sum(r["sold"] for r in rows)}
