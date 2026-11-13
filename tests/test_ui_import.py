"""The Import Data screen: a file is checked first, problems can be saved, Import works only on a clean check."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from core import auth, shop  # noqa: E402
from ui.screens import import_screen  # noqa: E402

HEAD = "name,size,colour,price,stock\n"


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn, owner, _ = shop.setup_new_shop(tmp_path / "shop", "owner", "owner-password", pin="1234")
    box = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: box.append(("info", a[2])) or QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: box.append(("warn", a[2])) or QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    yield conn, owner, tmp_path, box
    conn.close()


def _choose(monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))


def _csv(tmp_path, text, name="items.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_clean_file_is_checked_then_imported(world, monkeypatch):
    conn, owner, tmp, box = world
    screen = import_screen.build(conn, owner, None)
    assert not screen.btn_import.isEnabled() and not screen.btn_errors.isEnabled()
    _choose(monkeypatch, _csv(tmp, HEAD + "Shirt,M,Blue,500,3\nShirt,L,Blue,500,2\n"))
    screen.btn_choose.click()
    assert screen.btn_import.isEnabled() and screen.tbl_problems.rowCount() == 0
    assert "2 variant" in screen.lbl_summary.text() and "5 piece" in screen.lbl_summary.text()
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0        # only checked so far
    screen.btn_import.click()
    assert conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0] == 2
    assert box[-1][0] == "info" and "Imported 2 variant" in box[-1][1]
    assert not screen.btn_import.isEnabled()


def test_a_file_with_problems_cannot_be_imported_and_the_problems_can_be_saved(world, monkeypatch):
    conn, owner, tmp, box = world
    screen = import_screen.build(conn, owner, None)
    _choose(monkeypatch, _csv(tmp, HEAD + "Shirt,M,Blue,500,3\nBad,,Blue,500,1\n"))
    screen.btn_choose.click()
    assert not screen.btn_import.isEnabled() and screen.btn_errors.isEnabled()
    assert screen.tbl_problems.rowCount() == 1 and screen.tbl_problems.item(0, 0).text() == "3"
    assert "1 of 2 row" in screen.lbl_summary.text()
    out = tmp / "problems.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    screen.btn_errors.click()
    assert out.exists() and "This is required." in out.read_text(encoding="utf-8-sig")
    screen.do_import()                                    # even called directly, nothing is saved
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_a_wrong_file_shows_a_message_and_no_button_opens(world, monkeypatch):
    conn, owner, tmp, _ = world
    screen = import_screen.build(conn, owner, None)
    _choose(monkeypatch, _csv(tmp, "a,b\n1,2\n"))
    screen.btn_choose.click()
    assert "needs these columns" in screen.lbl_status.text()
    assert not screen.btn_import.isEnabled()


def test_the_example_file_can_be_saved_and_imported(world, monkeypatch):
    conn, owner, tmp, _ = world
    screen = import_screen.build(conn, owner, None)
    out = tmp / "example.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    screen.btn_template.click()
    _choose(monkeypatch, out)
    screen.btn_choose.click()
    assert screen.btn_import.isEnabled()


def test_a_cancelled_dialog_changes_nothing(world, monkeypatch):
    conn, owner, *_ = world
    screen = import_screen.build(conn, owner, None)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    screen.btn_choose.click()
    assert screen.lbl_file.text() and not screen.btn_import.isEnabled()


def test_the_menu_shows_import_only_to_people_who_may_edit_products(world):
    from ui import main_window as mw

    conn, owner, tmp, _ = world
    till = auth.create_user(conn, owner.user_id, "till", "till-pass-1", "cashier")
    assert any(text == "Import Data" and needs == ("edit_products",) for text, needs in mw.MENU)
    from core.permissions import has_permission

    assert not has_permission(conn, till, "edit_products") and has_permission(conn, owner.user_id, "edit_products")
    window = mw.MainWindow(conn, owner, {"name": "Shop"}, folder=tmp / "shop")
    assert "Import Data" in window.allowed
    window.close()
