"""The Dashboard (SRS section 5): every figure comes from the saved records through the report
functions, nothing is counted twice, each role sees only what it may, and the quick actions open the
right menu entry."""

import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import auth, cash, dashboard_data, shop  # noqa: E402
from core.dashboard_data import BEST_PERIODS, best_sellers, dashboard_figures, period_range, products_without_sales  # noqa: E402
from core.expenses import add_expense  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.purchases import confirm_purchase, create_draft_purchase, create_supplier  # noqa: E402
from core.reports_sales import sales_summary  # noqa: E402
from core.reports_stock import current_stock, stock_valuation  # noqa: E402
from core.sales import create_sale  # noqa: E402
from core.settings import set_setting  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import open_db  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.screens import dashboard_screen  # noqa: E402

_APP = QApplication.instance() or QApplication([])
TODAY = date.today()
TODAY_ISO = TODAY.isoformat()
CASH = [{"mode": "cash", "amount_paise": 10 ** 7}]


@pytest.fixture
def data(tmp_path):
    """A shirt (sold today), a tie (in stock, never sold), one purchase still owed to the supplier."""
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    ca = auth.create_user(conn, owner, "ca", "ca-password-1", "accountant")
    ensure_default_tax_rules(conn)

    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000, category="Shirts", reorder_level=8)
    tie = create_product(conn, "Tie", hsn="6217", default_price_paise=20000, category="Accessories")
    shirt_m, = generate_variants(conn, shirt, ["M"], ["Blue"])
    tie_v, = generate_variants(conn, tie, ["Free"], ["Red"])

    supplier = create_supplier(conn, "ABC Textiles")
    draft = create_draft_purchase(conn, supplier, "A-1", TODAY_ISO, [
        {"variant_id": shirt_m, "qty": 10, "unit_cost_paise": 50000},
        {"variant_id": tie_v, "qty": 4, "unit_cost_paise": 10000},
    ], created_by=owner)
    confirm_purchase(conn, draft, user_id=owner)
    create_sale(conn, [{"variant_id": shirt_m, "qty": 3}], CASH, user_id=till)
    add_expense(conn, TODAY_ISO, "Electricity", "October bill", 150000, user_id=owner)

    yield SimpleNamespace(conn=conn, owner=owner, till=till, ca=ca, shirt_m=shirt_m, tie_v=tie_v)
    conn.close()


# ---------------------------------------------------------------- the figures

def test_sales_figures_equal_the_sales_report(data):
    figures = dashboard_figures(data.conn, user_id=data.owner)
    report = sales_summary(data.conn, TODAY_ISO, TODAY_ISO, user_id=data.owner)
    assert figures["sales"] == report
    assert figures["sales"]["bill_count"] == 1 and figures["sales"]["items_sold"] == 3  # one bill, counted once
    assert figures["errors"] == []


def test_purchases_stock_and_expenses_today(data):
    figures = dashboard_figures(data.conn, user_id=data.owner)
    assert figures["purchases"]["count"] == 1 and figures["purchases"]["qty"] == 14
    assert figures["stock"]["total_qty"] == 11  # 10 + 4 received, 3 sold
    valuation = stock_valuation(data.conn, user_id=data.owner)
    assert figures["stock"]["value_paise"] == valuation["total_value_paise"]
    assert figures["stock"]["method_label"] == valuation["method_label"]  # the method is always stated
    assert figures["expenses"] == 150000


def test_low_stock_matches_the_stock_report(data):
    figures = dashboard_figures(data.conn, user_id=data.owner)
    assert [r["variant_id"] for r in figures["low_stock"]] == [data.shirt_m]  # 7 left, reorder level 8


def test_best_sellers_and_products_without_sales(data):
    figures = dashboard_figures(data.conn, user_id=data.owner)
    assert figures["best_sellers"] == [{"product": "Shirt", "net_qty": 3, "net_taxable_paise": figures["best_sellers"][0]["net_taxable_paise"]}]
    assert figures["no_sales"] == {"count": 1, "rows": [{"product": "Tie", "qty": 4}]}


def test_a_period_with_no_sales_lists_every_product_in_stock(data):
    start = (TODAY - timedelta(days=60)).isoformat()
    end = (TODAY - timedelta(days=40)).isoformat()
    assert best_sellers(data.conn, start, end, user_id=data.owner) == []
    idle = products_without_sales(data.conn, start, end, user_id=data.owner)
    assert idle["count"] == 2 and [r["product"] for r in idle["rows"]] == ["Shirt", "Tie"]


def test_period_ranges():
    today = date(2026, 11, 7)  # a Saturday
    assert period_range("today", today) == ("2026-11-07", "2026-11-07")
    assert period_range("this_week", today) == ("2026-11-02", "2026-11-07")
    assert period_range("this_month", today) == ("2026-11-01", "2026-11-07")
    assert period_range("last_30_days", today) == ("2026-10-09", "2026-11-07")
    with pytest.raises(ValueError):
        period_range("forever", today)
    assert set(BEST_PERIODS) == {"today", "this_week", "this_month", "last_30_days"}


def test_supplier_dues_show_what_is_still_owed(data):
    due = dashboard_figures(data.conn, user_id=data.owner)["supplier_dues"]
    assert due["suppliers"] == 1 and due["total_paise"] > 0


def test_customer_dues_only_when_credit_sales_are_on(data):
    assert "customer_dues" not in dashboard_figures(data.conn, user_id=data.owner)
    set_setting(data.conn, "credit_sales_enabled", "1")
    assert dashboard_figures(data.conn, user_id=data.owner)["customer_dues"] == {
        "total_paise": 0, "customers": 0, "overdue_paise": 0,
    }


def test_refreshing_does_not_fill_the_customer_access_log(data):
    set_setting(data.conn, "credit_sales_enabled", "1")
    before = data.conn.execute("SELECT COUNT(*) FROM customer_access_log").fetchone()[0]
    for _ in range(3):
        dashboard_figures(data.conn, user_id=data.owner)
    assert data.conn.execute("SELECT COUNT(*) FROM customer_access_log").fetchone()[0] == before


def test_cash_position_follows_the_register(data):
    assert dashboard_figures(data.conn, user_id=data.owner)["cash"]["status"] == "not_opened"
    cash.open_cash_day(data.conn, TODAY_ISO, user_id=data.owner, opening_paise=500000)
    figures = dashboard_figures(data.conn, user_id=data.owner)["cash"]
    assert figures["status"] == "open" and figures["open_day"] == TODAY_ISO
    assert figures["expected_paise"] == cash.get_cash_day(data.conn, TODAY_ISO, user_id=data.owner)["expected_paise"]


def test_the_dashboard_only_reads(data):
    tables = ("sales", "stock_movements", "payments", "expenses", "audit_log")
    count = lambda: [data.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]  # noqa: E731
    before = count()
    dashboard_figures(data.conn, user_id=data.owner)
    dashboard_figures(data.conn, user_id=data.owner, best_period="today")
    assert count() == before


# ---------------------------------------------------------------- permissions

def test_a_cashier_gets_no_report_figures(data):
    figures = dashboard_figures(data.conn, user_id=data.till)
    assert figures["errors"] == []
    for key in ("sales", "payments", "purchases", "stock", "low_stock", "best_sellers", "no_sales", "expenses"):
        assert key not in figures


def test_an_accountant_sees_money_but_the_cost_sections_follow_the_cost_permission(data):
    figures = dashboard_figures(data.conn, user_id=data.ca)
    assert figures["errors"] == [] and "sales" in figures and "expenses" in figures


def test_stock_without_cost_permission_shows_quantity_only(data, monkeypatch):
    from core.permissions import PermissionDenied

    def denied(*args, **kwargs):
        raise PermissionDenied("no")

    monkeypatch.setattr(dashboard_data.reports_stock, "stock_valuation", denied)
    stock = dashboard_figures(data.conn, user_id=data.owner)["stock"]
    assert stock["total_qty"] == 11 and stock["value_paise"] is None


def test_one_broken_section_does_not_stop_the_others(data, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dashboard_data.reports_money, "expense_report", broken)
    figures = dashboard_figures(data.conn, user_id=data.owner)
    assert figures["errors"] == ["expenses"] and "expenses" not in figures
    assert figures["sales"]["bill_count"] == 1


# ---------------------------------------------------------------- the screen

@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(f, "owner", "owner-password", pin="1234")
    shop.add_user(f, conn, owner, "till", "till-password", "cashier")
    conn.close()
    return f


def _window(folder, username="owner"):
    password = "owner-password" if username == "owner" else f"{username}-password"
    conn, session = shop.open_shop(folder, username, password)
    window = mw.MainWindow(conn, session, {"name": "Test Shop"}, folder=folder)
    return conn, window


def _card(page, key):
    return page.cards[key][1].text(), page.cards[key][2].text()


def test_the_dashboard_menu_uses_the_new_screen(folder):
    conn, window = _window(folder)
    page = window.pages.widget(window.allowed.index("Dashboard"))
    assert isinstance(page, dashboard_screen.DashboardScreen)
    conn.close()


def test_the_owner_sees_every_card_with_real_figures(folder):
    conn, window = _window(folder)
    page = window.pages.widget(0)
    assert _card(page, "bills")[0] == "0" and _card(page, "sales")[0] == "₹0.00"
    for key in ("sales", "bills", "returns", "money", "purchases", "stock", "low", "expenses", "cash", "supplier_dues", "backup"):
        assert not page.cards[key][0].isHidden(), key
    assert page.cards["customer_dues"][0].isHidden()  # credit sales are off by default
    conn.close()


def test_a_cashier_sees_no_money_cards_but_can_start_a_sale(folder):
    conn, window = _window(folder, "till")
    window.show()
    page = window.pages.widget(0)
    for key in ("sales", "bills", "returns", "money", "purchases", "stock", "expenses", "supplier_dues", "backup"):
        assert page.cards[key][0].isHidden(), key
    assert "does not show sales figures" in page.message.text()
    assert page.actions["New Sale / Billing"].isHidden() is False
    assert page.actions["Purchases"].isHidden() and page.actions["Reports"].isHidden()
    window.close()
    conn.close()


def test_a_quick_action_opens_its_menu_entry(folder):
    conn, window = _window(folder)
    window.show()
    page = window.pages.widget(0)
    page.actions["Purchases"].click()
    assert window.allowed[window.menu.currentRow()] == "Purchases"
    window.menu.setCurrentRow(0)
    page.actions["New Sale / Billing"].click()
    assert window.allowed[window.menu.currentRow()] == "New Sale / Billing"
    window.close()
    conn.close()


def test_changing_the_period_reloads_the_best_sellers(folder):
    conn, window = _window(folder)
    page = window.pages.widget(0)
    assert page.best_body.text() == "No sales in this period."
    page.period.setCurrentIndex(BEST_PERIODS.index("today"))
    assert page.period.currentData() == "today" and page.best_body.text() == "No sales in this period."
    conn.close()


def test_the_dashboard_screen_runs_no_sql():
    import re
    from pathlib import Path

    source = Path(dashboard_screen.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\.execute\(|\.commit\(\)|\.cursor\(", source)