"""The read functions the Payments and Cash screen uses (the screen holds no SQL)."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import cash
from core.permissions import PermissionDenied
from db.connection import open_db

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    for name, role in (("owner", "owner"), ("till", "cashier"), ("ca", "accountant")):
        conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    yield SimpleNamespace(conn=conn, owner=1, cashier=2, accountant=3)
    conn.close()


def test_nothing_is_open_or_carried_at_first(s):
    assert cash.get_open_day(s.conn, user_id=s.owner) is None
    assert cash.suggested_opening(s.conn, TODAY, user_id=s.owner) == 0
    assert cash.list_cash_days(s.conn, user_id=s.owner) == []
    assert cash.list_cash_movements(s.conn, TODAY, user_id=s.owner) == []


def test_open_day_movements_and_closing_are_listed(s):
    cash.open_cash_day(s.conn, YESTERDAY, user_id=s.owner, opening_paise=500000)
    assert cash.get_open_day(s.conn, user_id=s.owner) == YESTERDAY.isoformat()
    cash.add_cash_movement(s.conn, YESTERDAY, 20000, "Change from the bank", user_id=s.owner)
    cash.add_cash_movement(s.conn, YESTERDAY, -5000, "Tea for staff", user_id=s.owner)
    moves = cash.list_cash_movements(s.conn, YESTERDAY, user_id=s.owner)
    assert [(m["amount_paise"], m["reason"], m["user"]) for m in moves] == [
        (20000, "Change from the bank", "owner"), (-5000, "Tea for staff", "owner")]
    assert moves[0]["created_at"]
    cash.close_cash_day(s.conn, YESTERDAY, 515000, user_id=s.owner)
    assert cash.get_open_day(s.conn, user_id=s.owner) is None
    assert cash.suggested_opening(s.conn, TODAY, user_id=s.owner) == 515000  # the next day opens with what was counted
    cash.open_cash_day(s.conn, TODAY, user_id=s.owner)
    days = cash.list_cash_days(s.conn, user_id=s.owner)
    assert [d["business_date"] for d in days] == [TODAY.isoformat(), YESTERDAY.isoformat()]
    assert days[1]["status"] == "closed" and days[1]["counted_paise"] == 515000 and days[1]["difference_paise"] == 0
    assert days[0]["status"] == "open" and days[0]["opening_paise"] == 515000


def test_only_earlier_days_are_carried_forward(s):
    cash.open_cash_day(s.conn, TODAY, user_id=s.owner, opening_paise=1000)
    cash.close_cash_day(s.conn, TODAY, 1000, user_id=s.owner)
    assert cash.suggested_opening(s.conn, TODAY, user_id=s.owner) == 0


def test_the_history_can_be_limited(s):
    for i in range(5):
        day = TODAY - timedelta(days=10 - i)
        cash.open_cash_day(s.conn, day, user_id=s.owner, opening_paise=0)
        cash.close_cash_day(s.conn, day, 0, user_id=s.owner)
    assert len(cash.list_cash_days(s.conn, user_id=s.owner, limit=3)) == 3


def test_reading_needs_the_cash_or_reports_permission(s):
    cash.open_cash_day(s.conn, TODAY, user_id=s.owner, opening_paise=0)
    assert len(cash.list_cash_days(s.conn, user_id=s.accountant)) == 1  # view_reports is enough to look
    for call in (lambda: cash.list_cash_days(s.conn, user_id=s.cashier),
                 lambda: cash.get_open_day(s.conn, user_id=s.cashier),
                 lambda: cash.suggested_opening(s.conn, TODAY, user_id=s.cashier),
                 lambda: cash.list_cash_movements(s.conn, TODAY, user_id=s.cashier)):
        with pytest.raises(PermissionDenied):
            call()


def test_a_bad_date_is_refused(s):
    with pytest.raises(ValueError, match="valid date"):
        cash.list_cash_movements(s.conn, "31/12/2026", user_id=s.owner)
