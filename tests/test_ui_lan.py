"""The Second Counter screen (main PC) and the second PC's sign-in and menu."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import shop  # noqa: E402
from lan import service  # noqa: E402
from lan.client import RemoteConnection  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.login_page import LoginPage  # noqa: E402
from ui.screens import lan_screen  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(service, "local_addresses", lambda: ["127.0.0.1"])
    yield
    service.stop()


@pytest.fixture
def main_pc(tmp_path):
    folder = tmp_path / "shop"
    conn, session, _ = shop.setup_new_shop(folder, "owner", "Owner#12345", pin="1234")
    yield SimpleNamespace(folder=folder, conn=conn, session=session)
    service.stop()
    conn.close()


def test_owner_starts_and_stops_the_second_counter(main_pc):
    s = lan_screen.build(main_pc.conn, main_pc.session, main_pc.folder)
    assert service.current() is None and "OFF" in s.lbl_state.text()
    s.toggle()
    assert service.current() is not None and "ON" in s.lbl_state.text()
    assert service.current().host == "127.0.0.1"
    s.toggle()
    assert service.current() is None


def test_a_cashier_cannot_start_it(main_pc):
    shop.add_user(main_pc.folder, main_pc.conn, main_pc.session, "till", "Till#12345", "cashier")
    conn2, cashier = shop.open_shop(main_pc.folder, "till", "Till#12345")
    try:
        with pytest.raises(ValueError):
            service.start(conn2, cashier, main_pc.folder, host="127.0.0.1")
    finally:
        conn2.close()


def test_second_pc_signs_in_and_sees_only_the_billing_screens(main_pc, tmp_path):
    server = service.start(main_pc.conn, main_pc.session, main_pc.folder, host="127.0.0.1")
    code = service.pairing_code(main_pc.conn)
    page = LoginPage(tmp_path / "second_pc")                     # a PC with no shop of its own
    page.remote = {"host": "127.0.0.1", "port": server.port, "code": code}
    got = []
    page.signed_in.connect(lambda conn, session: got.append((conn, session)))
    page.username.setText("owner")
    page.password.setText("wrong-password")
    page._sign_in()
    assert not got and page.error.text()
    page.password.setText("Owner#12345")
    page._sign_in()
    assert len(got) == 1 and isinstance(got[0][0], RemoteConnection)
    conn, session = got[0]
    window = mw.MainWindow(conn, session, {"name": "Shop"}, folder=tmp_path / "second_pc", counter_mode=True)
    assert set(window.allowed) <= mw.COUNTER_MENU and "New Sale / Billing" in window.allowed
    assert "Backup and Restore" not in window.allowed and "Settings" not in window.allowed
    for index in range(window.pages.count()):
        window.menu.setCurrentRow(index)                          # every counter screen opens on the remote shop
    window.close()
    conn.close()


def test_main_pc_menu_has_the_second_counter(main_pc):
    window = mw.MainWindow(main_pc.conn, main_pc.session, {"name": "Shop"}, folder=main_pc.folder)
    assert "Second Counter" in window.allowed
    window.close()


def test_a_pc_with_no_shop_can_choose_second_counter(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog
    import ui.login_page as lp

    def fake_exec(self):
        self.link = {"host": "127.0.0.1", "port": 8765, "code": "K7M2-QX9P-4T"}
        return QDialog.Accepted

    monkeypatch.setattr(lp.CounterLinkDialog, "exec", fake_exec)
    controller = AppController(tmp_path / "empty")
    assert controller.current_name() == "setup"
    controller.setup_page.use_counter.emit()
    assert controller.current_name() == "login" and controller.login_page.remote["host"] == "127.0.0.1"
    controller.close()
