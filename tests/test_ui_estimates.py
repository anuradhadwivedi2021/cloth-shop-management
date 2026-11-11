"""The Estimates screen and the printed estimate."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from core import estimates as est  # noqa: E402
from core.estimate_document import build_estimate_document  # noqa: E402
from core.settings import set_setting  # noqa: E402
from ui import print_service  # noqa: E402
from ui.screens import estimates_screen  # noqa: E402
from tests.test_bill_document import _cart, shop  # noqa: E402,F401
from types import SimpleNamespace  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(shop):
    return SimpleNamespace(user_id=shop.user, touch=lambda: None, require=lambda *a, **k: None)


@pytest.fixture
def boxes(monkeypatch):
    seen = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: seen.append(("info", a[2])) or QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: seen.append(("warn", a[2])) or QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    return seen


def test_make_an_estimate_on_the_screen(shop, session, boxes):
    screen = estimates_screen.build(shop.conn, session, None)
    assert not screen.btn_save.isEnabled()
    assert screen.set_item(shop.m_blue) and screen.btn_add.isEnabled()
    screen.spin_qty.setValue(2)
    screen.btn_add.click()
    screen.btn_add.click()                                           # the same item again adds to the same line
    assert screen.tbl_cart.rowCount() == 1 and screen.tbl_cart.item(0, 1).text() == "4"
    screen.set_item(shop.l_black)
    screen.spin_qty.setValue(1)
    screen.btn_add.click()
    assert screen.tbl_cart.rowCount() == 2 and screen.btn_save.isEnabled() and "₹" in screen.lbl_total.text()
    screen.tbl_cart.selectRow(1)
    screen.btn_remove.click()
    assert screen.tbl_cart.rowCount() == 1
    screen.txt_customer.setText("Mrs Sharma")
    screen.btn_save.click()
    assert boxes[-1][0] == "info" and "EST/" in boxes[-1][1]
    assert screen.tbl_cart.rowCount() == 0 and not screen.btn_save.isEnabled()
    assert screen.tbl_list.rowCount() == 1 and screen.tbl_list.item(0, 2).text() == "Mrs Sharma"
    assert shop.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_list_buttons_follow_the_selected_estimate(shop, session, boxes):
    open_id = est.create_estimate(shop.conn, shop.user, _cart(shop))
    gone = est.create_estimate(shop.conn, shop.user, _cart(shop))
    est.cancel_estimate(shop.conn, shop.user, gone)
    screen = estimates_screen.build(shop.conn, session, None)
    assert screen.tbl_list.rowCount() == 2 and not screen.btn_convert.isEnabled()      # nothing selected
    screen.tbl_list.selectRow(0)                                                      # newest = cancelled
    assert screen.btn_print.isEnabled() and not screen.btn_convert.isEnabled() and not screen.btn_cancel.isEnabled()
    screen.tbl_list.selectRow(1)
    assert screen.btn_convert.isEnabled() and screen.btn_cancel.isEnabled()
    screen.btn_cancel.click()
    assert est.get_estimate(shop.conn, shop.user, open_id)["status"] == "cancelled"
    screen.cmb_filter.setCurrentIndex(screen.cmb_filter.findData("cancelled"))
    assert screen.tbl_list.rowCount() == 2


def test_a_price_change_is_shown_next_to_the_open_estimate(shop, session):
    from db.connection import transaction

    est.create_estimate(shop.conn, shop.user, _cart(shop))
    with transaction(shop.conn):
        shop.conn.execute("UPDATE products SET default_price_paise = 130000 WHERE name = 'Shirt'")
    screen = estimates_screen.build(shop.conn, session, None)
    screen.tbl_list.selectRow(0)
    assert "Prices have changed" in screen.lbl_detail.text()


def test_converting_through_the_payment_window_makes_the_bill(shop, session, boxes, monkeypatch):
    from ui import billing_counter_view as bcv

    eid = est.create_estimate(shop.conn, shop.user, _cart(shop))
    screen = estimates_screen.build(shop.conn, session, None)
    screen.tbl_list.selectRow(0)

    def fake_exec(dialog):                       # the cashier presses "Save bill" with the pre-filled cash amount
        dialog.submit()
        return 1

    monkeypatch.setattr(bcv.PaymentDialog, "exec", fake_exec)
    screen.btn_convert.click()
    assert shop.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
    assert est.get_estimate(shop.conn, shop.user, eid)["status"] == "converted"
    assert boxes[-1][0] == "info" and "INV/" in boxes[-1][1]


def test_printing_and_saving_an_estimate(shop, session, tmp_path, boxes, monkeypatch):
    eid = est.create_estimate(shop.conn, shop.user, _cart(shop), customer_name="Mrs Sharma", note="Wedding")
    out = tmp_path / "e.pdf"
    result = print_service.save_estimate_pdf(shop.conn, session, eid, str(out))
    assert result.ok and out.read_bytes()[:5] == b"%PDF-"
    folder = print_service.save_estimate_pdf(shop.conn, session, eid, str(tmp_path))
    assert folder.ok and folder.path.endswith("EST-26-27-000001.pdf") or folder.path.endswith(".pdf")
    assert not print_service.save_estimate_pdf(shop.conn, session, 999, str(tmp_path / "x.pdf")).ok

    def no_printer(bill, html):
        raise RuntimeError("no printer")

    monkeypatch.setattr(print_service, "_print_html", no_printer)
    failed = print_service.print_estimate(shop.conn, session, eid)
    assert not failed.ok and "No printer was found" in failed.message
    monkeypatch.setattr(print_service, "_print_html", lambda bill, html: "Test Printer")
    assert print_service.print_estimate(shop.conn, session, eid).ok
    assert shop.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0          # printing never makes a sale


def test_the_printed_estimate_says_what_it_is_in_each_language(shop):
    eid = est.create_estimate(shop.conn, shop.user, _cart(shop), customer_name="Mrs Sharma", note="Wedding", valid_days=10)
    doc = build_estimate_document(shop.conn, shop.user, eid)
    html = doc["html"]
    for text in ("Anu Cloth House", "ESTIMATE", "not a tax invoice", "EST/26-27/", "Mrs Sharma", "Shirt M Blue", "₹6,230.00",
                 "Wedding", "Valid until"):
        assert text in html, text
    assert "TAX INVOICE" not in html and "CGST" not in html
    set_setting(shop.conn, "bill_language", "hi")
    assert "टैक्स इनवॉइस नहीं" in build_estimate_document(shop.conn, shop.user, eid)["html"]
    set_setting(shop.conn, "bill_language", "mr")
    assert "टॅक्स इनव्हॉइस नाही" in build_estimate_document(shop.conn, shop.user, eid)["html"]


def test_the_menu_has_estimates_for_people_who_can_sell():
    from ui import main_window as mw

    assert any(text == "Estimates" and needs == ("sell",) for text, needs in mw.MENU)
