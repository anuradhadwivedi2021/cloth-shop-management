"""Read-only lists behind the Stock Count screen. They must not change anything and must respect permissions."""

import pytest

from core.permissions import PermissionDenied
from core.stock_counts import apply_count, count_categories, list_counts, record_count, start_count
from core.stock_ops import list_recent_adjustments, mark_damaged, mark_missing
from tests.test_returns import shop  # noqa: F401  (shop is a fixture: owner, cashier, 4 variants with 10 each)


def test_counts_are_listed_newest_first_with_progress(shop):
    s = shop
    assert list_counts(s.conn, user_id=s.owner) == []
    first = start_count(s.conn, user_id=s.owner)
    second = start_count(s.conn, user_id=s.owner, category="Shirts")
    record_count(s.conn, second, s.m_blue, 10, user_id=s.owner)
    rows = list_counts(s.conn, user_id=s.owner)
    assert [r["id"] for r in rows] == [second, first]
    assert rows[0]["counted_count"] == 1 and rows[0]["line_count"] == 2     # Shirt and Polo are in category Shirts
    assert rows[1]["counted_count"] == 0 and rows[1]["line_count"] == 4
    apply_count(s.conn, second, approver_id=s.owner)
    assert [r["id"] for r in list_counts(s.conn, user_id=s.owner, status="open")] == [first]
    with pytest.raises(ValueError):
        list_counts(s.conn, user_id=s.owner, status="weird")


def test_categories_come_from_active_variants(shop):
    s = shop
    assert count_categories(s.conn, user_id=s.owner) == ["Blazers", "Innerwear", "Shirts"]


def test_recent_adjustments_show_names_and_newest_first(shop):
    s = shop
    mark_damaged(s.conn, s.m_blue, 1, "torn", user_id=s.owner)
    mark_missing(s.conn, s.vest_m, 2, "not on shelf", user_id=s.owner)
    rows = list_recent_adjustments(s.conn, user_id=s.owner)
    assert [(r["kind"], r["product_name"], r["qty"]) for r in rows] == [("missing", "Vest", 2), ("damaged", "Shirt", 1)]
    assert rows[0]["username"] == "owner" and rows[0]["reason"] == "not on shelf"
    assert len(list_recent_adjustments(s.conn, user_id=s.owner, limit=1)) == 1


def test_a_cashier_cannot_use_the_lists(shop):
    s = shop
    for call in (lambda: list_counts(s.conn, user_id=s.cashier),
                 lambda: count_categories(s.conn, user_id=s.cashier),
                 lambda: list_recent_adjustments(s.conn, user_id=s.cashier)):
        with pytest.raises(PermissionDenied):
            call()