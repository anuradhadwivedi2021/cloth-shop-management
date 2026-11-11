"""BIL-12: estimates take no stock and make no sale; converting makes one real bill, once."""

from datetime import date, datetime

import pytest

from core import estimates as est
from core.audit import verify_chain
from core.permissions import PermissionDenied
from core.series_admin import KNOWN_SERIES, list_series
from core.shop_settings import update_shop_profile
from core.stock import InsufficientStockError, get_stock
from db.connection import transaction
from tests.test_bill_document import _cart, shop  # noqa: F401  (the same small shop)

TODAY = date(2026, 11, 1)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _make(s, **kwargs):
    kwargs.setdefault("today", TODAY)
    return est.create_estimate(s.conn, s.user, _cart(s), **kwargs)


# ---------------------------------------------------------------- making one

def test_an_estimate_has_its_own_number_and_takes_no_stock_and_makes_no_sale(shop):
    before = (get_stock(shop.conn, shop.m_blue), get_stock(shop.conn, shop.l_black))
    eid = _make(shop, customer_name="  Mrs   Sharma ", note="For the wedding")
    e = est.get_estimate(shop.conn, shop.user, eid, today=TODAY)
    assert e["estimate_no"] == "EST/26-27/000001" and e["status"] == "open" and e["customer_name"] == "Mrs Sharma"
    assert e["total_paise"] == 623000 and len(e["lines"]) == 2 and e["valid_until"] == "2026-11-08"
    assert [l["qty"] for l in e["lines"]] == [2, 1] and e["lines"][0]["unit_price_paise"] == 105000
    assert (get_stock(shop.conn, shop.m_blue), get_stock(shop.conn, shop.l_black)) == before
    assert (_count(shop.conn, "sales"), _count(shop.conn, "payments")) == (0, 0)
    assert est.get_estimate(shop.conn, shop.user, _make(shop), today=TODAY)["estimate_no"] == "EST/26-27/000002"
    assert verify_chain(shop.conn)[0] is True
    assert "EST" in KNOWN_SERIES and any(r["series"] == "EST" for r in list_series(shop.conn, TODAY))


def test_an_estimate_may_quote_more_pieces_than_are_in_stock(shop):
    big = est.create_estimate(shop.conn, shop.user, [{"variant_id": shop.m_blue, "qty": 500}], today=TODAY)
    assert est.get_estimate(shop.conn, shop.user, big, today=TODAY)["total_paise"] > 0


@pytest.mark.parametrize("cart, message", [
    ([], "at least one item"),
    ([{"variant_id": 1, "qty": 0}], "quantity"),
    ([{"variant_id": 1, "qty": True}], "quantity"),
    ([{"variant_id": 1, "qty": 1, "discount_paise": -5}], "negative"),
    ([{"variant_id": 999, "qty": 1}], "not found"),
])
def test_bad_estimates_are_refused_and_nothing_is_saved(shop, cart, message):
    with pytest.raises(ValueError, match=message):
        est.create_estimate(shop.conn, shop.user, cart, today=TODAY)
    assert _count(shop.conn, "estimates") == 0
    assert shop.conn.execute("SELECT COALESCE(SUM(last_number), 0) FROM number_series WHERE series = 'EST'").fetchone()[0] == 0


def test_validity_days_and_customer_are_checked(shop):
    for days in (0, 91, True, "7"):
        with pytest.raises(ValueError, match="valid for 1 to 90"):
            _make(shop, valid_days=days)
    with pytest.raises(ValueError, match="customer was not found"):
        _make(shop, customer_id=999)
    assert est.get_estimate(shop.conn, shop.user, _make(shop, valid_days=30), today=TODAY)["valid_until"] == "2026-12-01"


def test_only_people_who_can_sell_may_use_estimates(shop):
    shop.conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('keeper', 'x', 'stock')")
    keeper = shop.conn.execute("SELECT id FROM users WHERE username = 'keeper'").fetchone()[0]
    eid = _make(shop)
    for call in (lambda: est.create_estimate(shop.conn, keeper, _cart(shop)),
                 lambda: est.list_estimates(shop.conn, keeper),
                 lambda: est.get_estimate(shop.conn, keeper, eid),
                 lambda: est.cancel_estimate(shop.conn, keeper, eid),
                 lambda: est.convert_estimate(shop.conn, keeper, eid, [])):
        with pytest.raises(PermissionDenied):
            call()


# ---------------------------------------------------------------- reading, price change, expiry

def test_the_list_filters_open_expired_converted_and_cancelled(shop):
    open_id, old_id, cancel_id, bill_id = _make(shop), _make(shop, valid_days=1), _make(shop), _make(shop)
    est.cancel_estimate(shop.conn, shop.user, cancel_id)
    est.convert_estimate(shop.conn, shop.user, bill_id, [{"mode": "cash", "amount_paise": 623000}], today=TODAY)
    later = date(2026, 11, 5)          # the 1-day estimate is past its date, the 7-day one is not
    ids = lambda status: [r["id"] for r in est.list_estimates(shop.conn, shop.user, status=status, today=later)]  # noqa: E731
    assert ids("open") == [open_id] and ids("expired") == [old_id]
    assert ids("converted") == [bill_id] and ids("cancelled") == [cancel_id]
    assert len(ids(None)) == 4
    assert est.get_estimate(shop.conn, shop.user, old_id, today=later)["expired"] is True


def test_a_price_change_since_the_quote_is_flagged(shop):
    eid = _make(shop)
    assert est.get_estimate(shop.conn, shop.user, eid, today=TODAY)["price_changed"] is False
    with transaction(shop.conn):
        shop.conn.execute("UPDATE products SET default_price_paise = 120000 WHERE name = 'Shirt'")
    e = est.get_estimate(shop.conn, shop.user, eid, today=TODAY)
    assert e["price_changed"] is True and e["total_paise"] == 623000 and e["current_total_paise"] > 623000
    assert e["lines"][0]["unit_price_paise"] == 105000          # the quote itself never changes


# ---------------------------------------------------------------- converting

def test_converting_makes_one_normal_bill_and_closes_the_estimate(shop):
    eid = _make(shop, customer_name="Mrs Sharma")
    result = est.convert_estimate(shop.conn, shop.user, eid, [{"mode": "cash", "amount_paise": 623000}], today=TODAY)
    assert result["quoted_total_paise"] == result["billed_total_paise"] == 623000
    sale = shop.conn.execute("SELECT * FROM sales WHERE id = ?", (result["sale_id"],)).fetchone()
    assert sale["invoice_no"].startswith("INV/") and sale["total_paise"] == 623000
    assert (get_stock(shop.conn, shop.m_blue), get_stock(shop.conn, shop.l_black)) == (8, 9)      # stock moves only now
    e = est.get_estimate(shop.conn, shop.user, eid, today=TODAY)
    assert e["status"] == "converted" and e["sale_id"] == result["sale_id"] and e["closed_at"]
    assert verify_chain(shop.conn)[0] is True
    actions = [r[0] for r in shop.conn.execute("SELECT action FROM audit_log WHERE action LIKE 'estimate_%' ORDER BY id")]
    assert actions == ["estimate_created", "estimate_converted"]


def test_converting_twice_never_makes_a_second_bill(shop):
    eid = _make(shop)
    first = est.convert_estimate(shop.conn, shop.user, eid, [{"mode": "cash", "amount_paise": 623000}], today=TODAY)
    again = est.convert_estimate(shop.conn, shop.user, eid, [{"mode": "cash", "amount_paise": 623000}], today=TODAY)
    assert again["sale_id"] == first["sale_id"] and _count(shop.conn, "sales") == 1
    assert get_stock(shop.conn, shop.m_blue) == 8


def test_a_crash_between_the_bill_and_closing_the_estimate_is_repaired_by_retrying(shop):
    from core.guarded import sell

    eid = _make(shop)
    sell(shop.conn, [{"variant_id": shop.m_blue, "qty": 2}, {"variant_id": shop.l_black, "qty": 1}],
         [{"mode": "cash", "amount_paise": 623000}], user_id=shop.user, idempotency_key=f"estimate-{eid}",
         on_date=TODAY.isoformat())                                       # the bill was saved, then the power went
    assert est.get_estimate(shop.conn, shop.user, eid, today=TODAY)["status"] == "open"
    result = est.convert_estimate(shop.conn, shop.user, eid, [{"mode": "cash", "amount_paise": 623000}], today=TODAY)
    assert _count(shop.conn, "sales") == 1 and est.get_estimate(shop.conn, shop.user, eid, today=TODAY)["sale_id"] == result["sale_id"]


def test_conversion_uses_todays_price_and_says_so(shop):
    eid = _make(shop)
    with transaction(shop.conn):
        shop.conn.execute("UPDATE products SET default_price_paise = 110000 WHERE name = 'Shirt'")
    result = est.convert_estimate(shop.conn, shop.user, eid, [{"mode": "cash", "amount_paise": 700000}], today=TODAY)
    assert result["quoted_total_paise"] == 623000 and result["billed_total_paise"] != 623000


def test_conversion_fails_cleanly_when_stock_is_short_or_payment_is_wrong(shop):
    eid = est.create_estimate(shop.conn, shop.user, [{"variant_id": shop.m_blue, "qty": 11}], today=TODAY)   # 10 in stock
    total = est.get_estimate(shop.conn, shop.user, eid, today=TODAY)["total_paise"]
    with pytest.raises(InsufficientStockError):
        est.convert_estimate(shop.conn, shop.user, eid, [{"mode": "cash", "amount_paise": total}], today=TODAY)
    assert est.get_estimate(shop.conn, shop.user, eid, today=TODAY)["status"] == "open"
    assert (_count(shop.conn, "sales"), get_stock(shop.conn, shop.m_blue)) == (0, 10)
    ok = _make(shop)
    with pytest.raises(ValueError):
        est.convert_estimate(shop.conn, shop.user, ok, [{"mode": "cash", "amount_paise": 1000}], today=TODAY)   # underpaid, no credit
    assert est.get_estimate(shop.conn, shop.user, ok, today=TODAY)["status"] == "open" and _count(shop.conn, "sales") == 0


def test_expired_and_cancelled_estimates_cannot_be_converted(shop):
    old = _make(shop, valid_days=1)
    with pytest.raises(ValueError, match="expired"):
        est.convert_estimate(shop.conn, shop.user, old, [{"mode": "cash", "amount_paise": 623000}], today=date(2026, 11, 3))
    gone = _make(shop)
    est.cancel_estimate(shop.conn, shop.user, gone)
    with pytest.raises(ValueError, match="cancelled"):
        est.convert_estimate(shop.conn, shop.user, gone, [{"mode": "cash", "amount_paise": 623000}], today=TODAY)
    with pytest.raises(ValueError, match="Only an open"):
        est.cancel_estimate(shop.conn, shop.user, gone)
    with pytest.raises(ValueError, match="not found"):
        est.convert_estimate(shop.conn, shop.user, 999, [])
    assert _count(shop.conn, "sales") == 0


def test_the_database_refuses_to_edit_or_delete_what_was_quoted(shop):
    eid = _make(shop)
    for sql in ("DELETE FROM estimates", "DELETE FROM estimate_lines", "UPDATE estimate_lines SET qty = 9",
                "UPDATE estimates SET total_paise = 1", "UPDATE estimates SET valid_until = '2099-01-01'"):
        with pytest.raises(Exception, match="cannot|only be closed"):
            with transaction(shop.conn):
                shop.conn.execute(sql)
    est.cancel_estimate(shop.conn, shop.user, eid)
    with pytest.raises(Exception, match="closed estimate"):
        with transaction(shop.conn):
            shop.conn.execute("UPDATE estimates SET status = 'open' WHERE id = ?", (eid,))
