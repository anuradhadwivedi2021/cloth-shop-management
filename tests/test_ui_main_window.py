"""The main window must open for every role, and one broken screen must never stop the others."""

import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import shop  # noqa: E402
from core.guarded import sell  # noqa: E402
from core.money import format_rupees  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.stock import add_movement  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import main_window as mw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "owner-password"
ROLES = {"owner": "owner", "till": "cashier", "store": "stock", "ca": "accountant"}

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run (avoids a Qt exit crash)


def _dispose(widget):
    widget.setParent(None)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _APP.processEvents()


@pytest.fixture(scope="session")
def qapp():
    yield _APP
    for w in QApplication.topLevelWidgets():
        _dispose(w)
    gc.collect()


@pytest.fixture
def s(tmp_path, qapp):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", PASSWORD, pin="1234")
    for name, role in ROLES.items():
        if name != "owner":
            shop.add_user(folder, conn, owner, name, f"{name}-password", role)
    ensure_default_tax_rules(conn)
    product = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(conn, product, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 10, "opening stock")
    conn.close()
    return type("S", (), {"folder": folder, "variant": variant})


def _window(s, username):
    conn, session = shop.open_shop(s.folder, username, PASSWORD if username == "owner" else f"{username}-password")
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=s.folder)
    return conn, session, window


def _close(conn, window):
    _dispose(window)
    conn.close()


# ---------------------------------------------------------------- opens for every role

@pytest.mark.parametrize("username", list(ROLES))
def test_main_window_opens_and_every_page_opens_for_each_role(s, username):
    conn, session, window = _window(s, username)
    assert window.allowed and window.allowed[0] == "Dashboard"
    assert window.pages.count() == len(window.allowed) == window.menu.count()
    for row in range(window.menu.count()):
        window.menu.setCurrentRow(row)
        page = window.pages.widget(row)
        assert not getattr(page, "is_error", False), f"{window.allowed[row]} failed to build"
        assert window.pages.currentIndex() == row
    _close(conn, window)


def test_menu_follows_permissions(s):
    conn, _, owner_window = _window(s, "owner")
    assert [t for t, _ in mw.MENU] == owner_window.allowed  # the owner sees the whole SRS menu
    _close(conn, owner_window)

    conn, _, cashier_window = _window(s, "till")
    assert "New Sale / Billing" in cashier_window.allowed
    for hidden in ("Users and Permissions", "Settings", "Backup and Restore", "Reports", "Payments and Cash"):
        assert hidden not in cashier_window.allowed
    _close(conn, cashier_window)

    conn, _, ca_window = _window(s, "ca")
    assert "New Sale / Billing" not in ca_window.allowed and "Reports" in ca_window.allowed
    _close(conn, ca_window)


def test_connected_screens_are_the_real_ones(s):
    from ui.backup_view import BackupRestoreView
    from ui.dashboard_page import DashboardPage
    from ui.suppliers_view import SuppliersManagementView
    from ui.users_permissions_view import UsersAndPermissionsView

    conn, _, window = _window(s, "owner")
    expected = {
        "Dashboard": DashboardPage, "Backup and Restore": BackupRestoreView,
        "Suppliers": SuppliersManagementView, "Users and Permissions": UsersAndPermissionsView,
    }
    for text, cls in expected.items():
        assert isinstance(window.pages.widget(window.allowed.index(text)), cls), text
    _close(conn, window)


def test_screens_not_yet_on_the_tested_backend_stay_closed(s):
    """Billing must stay closed until it can only say 'saved' when the bill really is saved."""
    conn, _, window = _window(s, "owner")
    for text in mw.PENDING:
        page = window.pages.widget(window.allowed.index(text))
        assert isinstance(page, mw.PlaceholderPage) and not page.is_error, text
    _close(conn, window)


# ---------------------------------------------------------------- one broken screen never stops the rest

def test_a_screen_that_fails_to_build_shows_an_error_page_and_the_rest_work(s, monkeypatch):
    import ui.suppliers_view as suppliers

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(suppliers, "SuppliersManagementView", boom)
    conn, _, window = _window(s, "owner")
    broken = window.pages.widget(window.allowed.index("Suppliers"))
    assert broken.is_error is True
    from PySide6.QtWidgets import QLabel
    shown = " ".join(w.text() for w in broken.findChildren(QLabel))
    assert "could not be opened" in shown and "secret internal detail" not in shown  # no technical detail on screen
    ok = window.pages.widget(window.allowed.index("Dashboard"))
    assert not getattr(ok, "is_error", False)
    _close(conn, window)


def test_main_window_import_never_pulls_in_tkinter():
    code = "import sys, ui.main_window; print('tkinter' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert out.stdout.strip() == "False", out.stderr


# ---------------------------------------------------------------- dashboard figures

def test_dashboard_shows_todays_figures_from_the_reports(s):
    conn, session, window = _window(s, "owner")
    sell(conn, [{"variant_id": s.variant, "qty": 2}], [{"mode": "cash", "amount_paise": 210000}],
         user_id=session.user_id, mode="unregistered")
    dash = window.pages.widget(0)
    dash.refresh()
    assert dash.bills_value.text() == "1"
    assert dash.sales_value.text() == format_rupees(210000)
    assert dash.money_value.text() == format_rupees(210000) and "Cash" in dash.money_note.text()
    assert dash.stock_value.text() == "0"
    assert "never" not in dash.backup_value.text().lower() or dash.backup_value.text() == "Never"
    _close(conn, window)


def test_dashboard_hides_sales_figures_from_a_cashier(s):
    conn, _, window = _window(s, "till")
    dash = window.pages.widget(0)
    assert all(card.isHidden() for card in dash.report_cards)
    assert dash.backup_card.isHidden()
    assert "does not show sales figures" in dash.message.text()
    _close(conn, window)


def test_dashboard_shows_backup_age_to_the_owner(s):
    from core import backup
    conn, session, window = _window(s, "owner")
    dash = window.pages.widget(0)
    assert dash.backup_value.text() == "Never"
    backup.create_backup(conn, session, s.folder, s.folder.parent / "usb")
    dash.refresh()
    assert dash.backup_value.text() == "0 day(s) ago"
    _close(conn, window)
