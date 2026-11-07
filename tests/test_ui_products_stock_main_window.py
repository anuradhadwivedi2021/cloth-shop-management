"""Products and Stock and Variants are connected in the main window (and no longer 'pending')."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import shop  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.stock import add_movement  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.screens.products_screen import ProductsScreen  # noqa: E402
from ui.screens.stock_screen import StockScreen  # noqa: E402

_APP = QApplication.instance() or QApplication([])
PASSWORD = "owner-password"


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(f, "owner", PASSWORD, pin="1234")
    shop.add_user(f, conn, owner, "store", "store-password", "stock")
    shop.add_user(f, conn, owner, "till", "till-password", "cashier")
    shop.add_user(f, conn, owner, "ca", "ca-password", "accountant")
    product = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(conn, product, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 7, "opening stock")
    conn.close()
    return f


def _window(folder, username):
    pw = PASSWORD if username == "owner" else f"{username}-password"
    conn, session = shop.open_shop(folder, username, pw)
    return conn, mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)


@pytest.mark.parametrize("username", ["owner", "store", "till", "ca"])
def test_both_screens_are_the_real_ones(folder, username):
    conn, window = _window(folder, username)
    assert isinstance(window.pages.widget(window.allowed.index("Products")), ProductsScreen)
    assert isinstance(window.pages.widget(window.allowed.index("Stock and Variants")), StockScreen)
    conn.close()


def test_not_listed_as_pending_any_more():
    assert "Products" not in mw.PENDING and "Stock and Variants" not in mw.PENDING


@pytest.mark.parametrize("username", ["owner", "store", "till"])
def test_stock_screen_shows_quantity_without_view_reports(folder, username):
    conn, window = _window(folder, username)
    page = window.pages.widget(window.allowed.index("Stock and Variants"))
    assert page.table.rowCount() == 1
    assert page.table.item(0, 6).text() == "7"
    conn.close()


def test_products_list_opens_detail_and_returns(folder):
    conn, window = _window(folder, "owner")
    page = window.pages.widget(window.allowed.index("Products"))
    assert page.table.rowCount() == 1 and page.table.item(0, 0).text() == "Shirt"
    page.table.selectRow(0)
    page._open_selected()
    assert page.stack.currentIndex() == 1 and page._detail.product_id == page._ids[0]
    page._close_detail()
    assert page.stack.currentIndex() == 0 and page._detail is None
    conn.close()


def test_cashier_cannot_add_products(folder):
    conn, window = _window(folder, "till")
    assert not window.pages.widget(window.allowed.index("Products")).new_btn.isEnabled()
    conn.close()


def test_search_filters(folder):
    conn, window = _window(folder, "owner")
    page = window.pages.widget(window.allowed.index("Products"))
    page.search.setText("zzz-nothing")
    assert page.table.rowCount() == 0
    page.search.setText("blue")
    assert page.table.rowCount() == 1
    conn.close()
