import csv
from datetime import date
from types import SimpleNamespace

import pytest

from core.cash import add_cash_movement, close_cash_day, open_cash_day
from core.expenses import add_expense, reverse_expense
from core.permissions import PermissionDenied
from core.report_export import export_report_csv
from core.reports_money import cash_book, expense_report
from db.connection import open_db

TODAY = date.today().isoformat()


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('till', 'x', 'cashier')")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('ca', 'x', 'accountant')")
    yield SimpleNamespace(conn=conn, owner=1, cashier=2, accountant=3, tmp=tmp_path)
    conn.close()


def _pay(conn, doc_type, amount, mode="cash", day=None):
    """A payment row as the sales, purchase and return code save it (optionally dated on another day)."""
    if day is None:
        conn.execute(
            "INSERT INTO payments(document_type, document_id, mode, amount_paise) VALUES (?, 1, ?, ?)",
            (doc_type, mode, amount),
        )
    else:
        conn.execute(
            "INSERT INTO payments(document_type, document_id, mode, amount_paise, created_at) "
            "VALUES (?, 1, ?, ?, ?)",
            (doc_type, mode, amount, f"{day} 11:00:00"),
        )


def _busy_day(s):
    """Opening 1000.00 rupees, then every kind of cash movement. Expected cash is 1170.00 rupees."""
    open_cash_day(s.conn, TODAY, user_id=s.owner, opening_paise=100000)
    _pay(s.conn, "sale", 50000)             # cash received on a bill
    _pay(s.conn, "sale_return", -10000)     # cash refunded to a customer
    _pay(s.conn, "purchase", 20000)         # cash paid to a supplier
    add_expense(s.conn, TODAY, "Rent", "Shop rent", 5000, user_id=s.owner)  # cash expense
    add_cash_movement(s.conn, TODAY, 3000, "float from owner", user_id=s.owner)
    add_cash_movement(s.conn, TODAY, -1000, "tea money", user_id=s.owner)


# ---------- cash book ----------

def test_cash_book_day_row_matches_the_register(shop):
    _busy_day(shop)
    book = cash_book(shop.conn, TODAY, TODAY, user_id=shop.owner)
    (day,) = book["days"]
    assert day["status"] == "open"
    assert (day["opening_paise"], day["cash_sales_paise"], day["customer_refunds_paise"]) == (100000, 50000, 10000)
    assert (day["supplier_payments_paise"], day["expenses_paise"]) == (20000, 5000)
    assert (day["manual_in_paise"], day["manual_out_paise"]) == (3000, 1000)
    assert day["expected_paise"] == 117000


def test_running_balance_ends_at_expected_cash(shop):
    _busy_day(shop)
    book = cash_book(shop.conn, TODAY, TODAY, user_id=shop.owner)
    assert len(book["entries"]) == 6
    assert book["entries"][-1]["balance_paise"] == book["days"][0]["expected_paise"] == 117000
    balance = 100000
    for e in book["entries"]:
        balance += e["cash_in_paise"] - e["cash_out_paise"]
        assert e["balance_paise"] == balance
        assert (e["cash_in_paise"] == 0) or (e["cash_out_paise"] == 0)  # a line is either money in or money out
    totals = book["totals"]
    assert (totals["cash_in_paise"], totals["cash_out_paise"], totals["net_change_paise"]) == (53000, 36000, 17000)
    assert book["formula"].startswith("Expected cash = opening cash")


def test_only_cash_payments_are_in_the_cash_book(shop):
    open_cash_day(shop.conn, TODAY, user_id=shop.owner, opening_paise=0)
    _pay(shop.conn, "sale", 70000, mode="upi")
    _pay(shop.conn, "sale", 30000, mode="card")
    _pay(shop.conn, "sale", 9000, mode="cash")
    book = cash_book(shop.conn, TODAY, TODAY, user_id=shop.owner)
    assert [e["cash_in_paise"] for e in book["entries"]] == [9000]
    assert book["days"][0]["expected_paise"] == 9000


def test_closed_day_shows_the_difference_and_reason_without_changing_figures(shop):
    _busy_day(shop)
    close_cash_day(shop.conn, TODAY, 116500, user_id=shop.owner, reason="note torn")
    book = cash_book(shop.conn, TODAY, TODAY, user_id=shop.accountant)
    (day,) = book["days"]
    assert (day["status"], day["counted_paise"], day["difference_paise"]) == ("closed", 116500, -500)
    assert day["difference_reason"] == "note torn"
    assert day["expected_paise"] == 117000  # never adjusted to match the count
    assert book["totals"]["difference_paise"] == -500
    assert (book["totals"]["closed_days"], book["totals"]["days_with_difference"]) == (1, 1)


def test_period_filter_keeps_other_days_out(shop):
    _pay(shop.conn, "sale", 11100, day="2026-10-01")
    _pay(shop.conn, "sale", 22200, day="2026-10-02")
    _pay(shop.conn, "sale", 33300, day="2026-10-03")
    book = cash_book(shop.conn, "2026-10-02", "2026-10-02", user_id=shop.owner)
    assert [d["business_date"] for d in book["days"]] == ["2026-10-02"]
    assert [e["cash_in_paise"] for e in book["entries"]] == [22200]
    assert book["days"][0]["status"] == "not_opened"  # cash was taken on a day the register was never opened


def test_each_day_has_its_own_running_balance(shop):
    _pay(shop.conn, "sale", 10000, day="2026-10-01")
    _pay(shop.conn, "sale", 5000, day="2026-10-02")
    book = cash_book(shop.conn, "2026-10-01", "2026-10-02", user_id=shop.owner)
    assert [e["balance_paise"] for e in book["entries"]] == [10000, 5000]


def test_reversed_cash_expense_returns_cash_in_the_book(shop):
    open_cash_day(shop.conn, TODAY, user_id=shop.owner, opening_paise=10000)
    eid = add_expense(shop.conn, TODAY, "Repairs", "Fan", 4000, user_id=shop.owner)
    reverse_expense(shop.conn, eid, "entered twice", user_id=shop.owner)
    book = cash_book(shop.conn, TODAY, TODAY, user_id=shop.owner)
    assert [(e["cash_in_paise"], e["cash_out_paise"]) for e in book["entries"]] == [(0, 4000), (4000, 0)]
    assert book["entries"][1]["description"].startswith("Reversal.")
    assert book["entries"][-1]["balance_paise"] == book["days"][0]["expected_paise"] == 10000


def test_cash_book_needs_the_view_reports_permission(shop):
    with pytest.raises(PermissionDenied):
        cash_book(shop.conn, TODAY, TODAY, user_id=shop.cashier)
    assert cash_book(shop.conn, TODAY, TODAY, user_id=shop.accountant)["days"] == []


def test_bad_dates_are_rejected(shop):
    with pytest.raises(ValueError):
        cash_book(shop.conn, "2020-11-05", "2020-11-01", user_id=shop.owner)
    with pytest.raises(ValueError):
        expense_report(shop.conn, "01-11-2026", "2020-11-30", user_id=shop.owner)


# ---------- expense report ----------

def _expenses(s):
    rent = add_expense(s.conn, "2020-11-01", "Rent", "November rent", 500000, user_id=s.owner, payment_mode="upi")
    add_expense(s.conn, "2020-11-10", "Electricity", "October bill", 120000, user_id=s.owner)
    add_expense(s.conn, "2020-12-02", "Rent", "December rent", 500000, user_id=s.owner, payment_mode="upi")
    return rent


def test_expense_report_lists_oldest_first_with_net_totals(shop):
    _expenses(shop)
    report = expense_report(shop.conn, "2020-11-01", "2020-12-31", user_id=shop.owner)
    assert [r["date"] for r in report["rows"]] == ["2020-11-01", "2020-11-10", "2020-12-02"]
    assert report["totals"] == {"line_count": 3, "recorded_paise": 1120000, "reversed_paise": 0, "net_paise": 1120000}
    assert report["by_category"] == [
        {"key": "Electricity", "count": 1, "total_paise": 120000},
        {"key": "Rent", "count": 2, "total_paise": 1000000},
    ]
    assert [(m["key"], m["total_paise"]) for m in report["by_month"]] == [("2020-11", 620000), ("2020-12", 500000)]
    assert [(m["key"], m["total_paise"]) for m in report["by_payment_mode"]] == [("cash", 120000), ("upi", 1000000)]
    assert "Net expenses" in report["formula"]


def test_reversed_expense_is_shown_and_netted(shop):
    rent = _expenses(shop)
    reverse_expense(shop.conn, rent, "entered twice", user_id=shop.owner)
    report = expense_report(shop.conn, "2020-11-01", "2020-11-30", user_id=shop.owner)
    statuses = {r["id"]: r["status"] for r in report["rows"]}
    assert sorted(statuses.values()) == ["active", "reversal", "reversed"]
    assert report["totals"] == {"line_count": 3, "recorded_paise": 620000, "reversed_paise": 500000, "net_paise": 120000}
    assert {c["key"]: c["total_paise"] for c in report["by_category"]} == {"Rent": 0, "Electricity": 120000}


def test_expense_report_category_filter_and_period(shop):
    _expenses(shop)
    only_rent = expense_report(shop.conn, "2020-11-01", "2020-12-31", user_id=shop.owner, category="rent")
    assert [r["category"] for r in only_rent["rows"]] == ["Rent", "Rent"]
    assert only_rent["totals"]["net_paise"] == 1000000
    nothing = expense_report(shop.conn, "2027-01-01", "2027-01-31", user_id=shop.owner)
    assert nothing["rows"] == [] and nothing["totals"]["net_paise"] == 0


def test_expense_report_matches_the_existing_summary(shop):
    from core.expenses import expense_summary
    rent = _expenses(shop)
    reverse_expense(shop.conn, rent, "entered twice", user_id=shop.owner)
    report = expense_report(shop.conn, "2020-11-01", "2020-12-31", user_id=shop.owner)
    summary = expense_summary(shop.conn, "2020-11-01", "2020-12-31", user_id=shop.owner)
    assert report["totals"]["net_paise"] == summary["total_paise"]


def test_expense_report_permissions(shop):
    _expenses(shop)
    with pytest.raises(PermissionDenied):
        expense_report(shop.conn, "2020-11-01", "2020-11-30", user_id=shop.cashier)
    assert expense_report(shop.conn, "2020-11-01", "2020-11-30", user_id=shop.accountant)["totals"]["line_count"] == 2


# ---------- export ----------

def test_both_reports_export_to_csv(shop):
    _busy_day(shop)
    _expenses(shop)
    book = cash_book(shop.conn, TODAY, TODAY, user_id=shop.owner)
    path = export_report_csv(shop.conn, shop.tmp / "cash.csv", book, user_id=shop.owner, report_name="cash_book",
                             columns=["date", "time", "number", "description", "cash_in_paise", "cash_out_paise",
                                      "balance_paise"])
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][-3:] == ["cash_in (Rs)", "cash_out (Rs)", "balance (Rs)"]
    assert len(rows) == 1 + 6 and rows[-1][-1] == "1170.00"

    report = expense_report(shop.conn, "2020-11-01", "2020-12-31", user_id=shop.owner)
    path = export_report_csv(shop.conn, shop.tmp / "expenses.csv", report, user_id=shop.owner, report_name="expense_report",
                             columns=["date", "category", "description", "payment_mode", "amount_paise", "status"])
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 1 + 3 and rows[1][4] == "5000.00"

    with pytest.raises(PermissionDenied):  # an accountant may export, a cashier may not even see the report
        export_report_csv(shop.conn, shop.tmp / "no.csv", book, user_id=shop.cashier, report_name="cash_book")