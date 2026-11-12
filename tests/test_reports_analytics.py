"""Sell-through % per product."""

from datetime import date

from core.reports_analytics import sell_through
from core.sales import create_sale
from tests.test_bill_document import _cart, shop  # noqa: F401

TODAY = date.today().isoformat()


def test_sell_through_per_product(shop):
    create_sale(shop.conn, _cart(shop), [{"mode": "cash", "amount_paise": 623000}], user_id=1)   # 2 shirts, 1 blazer
    rep = sell_through(shop.conn, TODAY, TODAY, user_id=1)
    rows = {r["product"]: r for r in rep["rows"]}
    assert rows["Shirt"]["sold"] == 2 and rows["Shirt"]["available"] == 10 and rows["Shirt"]["sell_through_percent"] == "20.0"
    assert rows["Blazer"]["sell_through_percent"] == "10.0" and rows["Blazer"]["closing"] == 9
    assert [r["product"] for r in rep["rows"]] == ["Shirt", "Blazer"]           # best seller first
    assert rep["total_sold"] == 3 and rep["total_available"] == 20
