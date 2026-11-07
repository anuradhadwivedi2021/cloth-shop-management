"""Purchases and Purchase Returns are connected in the main window (and no longer 'pending')."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import shop  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.purchase_returns_view import PurchaseReturnsView  # noqa: E402
from ui.purchases_view import PurchasesView  # noqa: E402

_APP = QApplication.instance() or QApplication([])
PASSWORD = "owner-password"


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(f, "owner", PASSWORD, pin="1234")
    shop.add_user(f, conn, owner, "store", "store-password", "stock")
    shop.add_user(f, conn, owner, "till", "till-password", "cashier")
    conn.close()
    return f


def _window(folder, username):
    conn, session = shop.open_shop(folder, username, PASSWORD if username == "owner" else f"{username}-password")
    return conn, mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)


@pytest.mark.parametrize("username", ["owner", "store"])
def test_both_screens_are_the_real_ones(folder, username):
    conn, window = _window(folder, username)
    assert isinstance(window.pages.widget(window.allowed.index("Purchases")), PurchasesView)
    assert isinstance(window.pages.widget(window.allowed.index("Purchase Returns")), PurchaseReturnsView)
    conn.close()


def test_they_are_not_listed_as_pending_any_more():
    assert "Purchases" not in mw.PENDING and "Purchase Returns" not in mw.PENDING


def test_a_cashier_does_not_see_them(folder):
    conn, window = _window(folder, "till")
    assert "Purchases" not in window.allowed and "Purchase Returns" not in window.allowed
    conn.close()