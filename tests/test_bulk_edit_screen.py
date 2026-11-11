"""PRD-09 screen: select products, preview, apply. And the text -> change helper."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import auth, bulk_edit as be, product_edit as pe, shop  # noqa: E402
from ui.screens import bulk_edit_screen  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn, owner, _ = shop.setup_new_shop(tmp_path / "shop", "owner", "owner-password", pin="1234")
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    ids = [pe.add_product(conn, owner.user_id, n, category="Shirt", default_price_paise=p, default_cost_paise=p // 2)["product_id"]
           for n, p in (("Blue shirt", 100000), ("Red shirt", 80000))]
    yield conn, owner, ids
    conn.close()


def _screen(world):
    conn, owner, _ = world
    return bulk_edit_screen.BulkEditScreen(conn, owner)


def test_build_changes_reads_typed_text():
    assert be.build_changes(season="Winter", gst_text="5", reorder_text="3", price_mode="set", price_text="499.50") == {
        "season": "Winter", "gst_rate_percent": 5, "reorder_level": 3, "set_price_paise": 49950}
    assert be.build_changes(price_mode="percent", price_text="-10") == {"price_change_percent": -10.0}
    for bad in (dict(), dict(gst_text="x"), dict(reorder_text="1.5"), dict(price_mode="set", price_text=""),
                dict(price_mode="set", price_text="1.234"), dict(price_mode="percent", price_text="abc")):
        with pytest.raises(ValueError):
            be.build_changes(**bad)


def test_preview_then_apply_changes_only_selected_products(world):
    conn, owner, ids = world
    s = _screen(world)
    assert s.tbl.rowCount() == 2
    s.tbl.selectRow(0)
    s.season.setText("Winter")
    s.do_preview()
    assert s.tbl_preview.rowCount() == 1 and s.btn_apply.isEnabled()
    assert conn.execute("SELECT season FROM products WHERE id=?", (ids[0],)).fetchone()[0] in (None, "")   # preview saved nothing
    s.do_apply()
    seasons = {r[0]: r[1] for r in conn.execute("SELECT id, season FROM products")}
    assert seasons[ids[0]] == "Winter" and seasons[ids[1]] in (None, "")
    assert not s.btn_apply.isEnabled()


def test_percent_price_shows_old_and_new_in_rupees(world):
    s = _screen(world)
    s.tbl.selectAll()
    s.price_mode.setCurrentIndex(2)
    s.price.setText("-10")
    s.do_preview()
    cells = {(s.tbl_preview.item(r, 0).text(), s.tbl_preview.item(r, 3).text()) for r in range(s.tbl_preview.rowCount())}
    assert any("900" in new for _n, new in cells) and any("720" in new for _n, new in cells)


def test_editing_after_preview_disables_apply_and_nothing_selected_is_an_error(world):
    s = _screen(world)
    s.tbl.selectRow(0)
    s.season.setText("Summer")
    s.do_preview()
    assert s.btn_apply.isEnabled()
    s.season.setText("Winter")
    assert not s.btn_apply.isEnabled()
    s.tbl.clearSelection()
    s.do_preview()
    assert s.lbl_status.text() and not s.btn_apply.isEnabled()


def test_a_user_without_permission_cannot_search(world):
    conn, owner, _ = world
    cashier = auth.create_user(conn, owner.user_id, "cash", "cash-password-1", role="cashier")
    with pytest.raises(ValueError):
        be.find_products(conn, cashier, "")
