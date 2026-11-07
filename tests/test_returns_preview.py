"""The read-only helpers behind the Sales Returns screen: nothing they do may change the database."""

import pytest

from core.permissions import PermissionDenied
from core.returns import preview_return, process_return, get_return
from core.stock_ops import list_returned_pending, review_returned
from tests.test_returns import DAY, _count, _item, _ret, _sell, shop  # noqa: F401  (shop is a fixture)


def _one_shirt(s):
    return _sell(s, cart=[{"variant_id": s.m_blue, "qty": 1}], payments=[{"mode": "cash", "amount_paise": 105000}])


def test_preview_of_a_plain_return_matches_what_is_saved(shop):
    s = shop
    sale = _sell(s)
    items = [_item(s, sale, s.m_blue, 1)]
    before = (_count(s, "sale_returns"), _count(s, "stock_movements"), _count(s, "payments"))
    preview = preview_return(s.conn, sale, items)
    assert (_count(s, "sale_returns"), _count(s, "stock_movements"), _count(s, "payments")) == before
    assert preview["replacement_total_paise"] is None and preview["difference_paise"] is None
    saved = get_return(s.conn, _ret(s, sale, items))["return"]
    assert preview["returned_value_paise"] == saved["total_paise"]
    assert preview["credit_paise"] == saved["refund_paise"]


def test_preview_difference_for_a_dearer_and_a_cheaper_exchange(shop):
    s = shop
    sale = _one_shirt(s)
    items = [_item(s, sale, s.m_blue, 1)]
    dearer = preview_return(s.conn, sale, items, exchange_cart=[{"variant_id": s.polo_m, "qty": 1}])
    assert dearer["credit_paise"] == 105000 and dearer["replacement_total_paise"] == 135000
    assert dearer["difference_paise"] == 30000          # customer pays Rs 300
    cheaper = preview_return(s.conn, sale, items, exchange_cart=[{"variant_id": s.vest_m, "qty": 1}])
    assert cheaper["difference_paise"] == -20000        # shop gives back Rs 200


def test_preview_follows_earlier_returns_and_rejects_too_many(shop):
    s = shop
    sale = _sell(s)
    _ret(s, sale, [_item(s, sale, s.m_blue, 1)])
    with pytest.raises(ValueError, match="can still be returned"):
        preview_return(s.conn, sale, [_item(s, sale, s.m_blue, 2)])
    with pytest.raises(ValueError, match="at least one"):
        preview_return(s.conn, sale, [])
    ok = preview_return(s.conn, sale, [_item(s, sale, s.m_blue, 1)])
    assert ok["returned_value_paise"] == 105000


def test_returned_items_wait_in_a_list_until_checked(shop):
    s = shop
    assert list_returned_pending(s.conn, user_id=s.owner) == []
    sale = _sell(s)
    _ret(s, sale, [_item(s, sale, s.m_blue, 2)])
    (row,) = list_returned_pending(s.conn, user_id=s.owner)
    assert row["variant_id"] == s.m_blue and row["qty"] == 2 and row["size"] == "M"
    review_returned(s.conn, s.m_blue, 2, resellable=True, user_id=s.owner)
    assert list_returned_pending(s.conn, user_id=s.owner) == []


def test_a_cashier_cannot_see_the_waiting_list(shop):
    with pytest.raises(PermissionDenied):
        list_returned_pending(shop.conn, user_id=shop.cashier)