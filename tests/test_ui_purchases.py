"""The Purchases and Purchase Returns screens use the tested core code: they say "saved" only when it
really was, show no technical detail, and cannot do what the user's permissions forbid."""

import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import purchase_service as svc, shop  # noqa: E402
from core.money import format_rupees  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.purchases import create_supplier  # noqa: E402
from core.stock import get_stock  # noqa: E402
from ui.purchase_returns_view import PurchaseReturnsView  # noqa: E402
from ui.purchases_view import GridDialog, PurchasesView, ReviewDialog  # noqa: E402

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run
PASSWORD = "owner-password"


@pytest.fixture
def s(tmp_path):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", PASSWORD, pin="1234")
    shop.add_user(folder, conn, owner, "store", "store-password", "stock")
    supplier = create_supplier(conn, "Ravi Textiles", phone="9999999999")
    product = create_product(conn, "Shirt", default_cost_paise=50000, gst_rate_percent=5)
    ids = generate_variants(conn, product, ["S", "M"], ["Blue", "Red"])
    conn.close()
    return SimpleNamespace(folder=folder, supplier=supplier, product=product, variants=ids)


def _open(s, username="owner", screen=PurchasesView):
    conn, session = shop.open_shop(s.folder, username, PASSWORD if username == "owner" else f"{username}-password")
    view = screen(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], asks=[], answer=True)
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def ask(title, text):
        shown.asks.append(text)
        return shown.answer

    view._ask = ask
    return conn, view, shown


def _fill_invoice(view, s, number="INV-1"):
    view.cmb_supplier.setCurrentIndex(view.cmb_supplier.findData(s.supplier))
    view.txt_invoice.setText(number)
    view.txt_find.setText("shirt")
    view.find_products()
    view._open_grid = lambda matrix: svc.lines_from_matrix(
        matrix, {s.variants[0]: 4, s.variants[3]: 6}, unit_cost_paise=50000, tax_rate_percent=5.0)
    view.add_grid()


def _stock(conn, s):
    return [get_stock(conn, v) for v in s.variants]


# ---------------------------------------------------------------- entering an invoice

def test_grid_lines_are_worked_out_and_shown_in_rupees(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s)
    assert view.tbl_lines.rowCount() == 2
    # 10 pieces x Rs 500 = 5,000 + 5% GST 250 = 5,250
    assert view.totals["subtotal"].text() == format_rupees(500000)
    assert view.totals["tax"].text() == format_rupees(25000)
    assert view.totals["total"].text() == format_rupees(525000)
    assert view.totals["pieces"].text() == "10"
    view.txt_paid.setText("1000")
    assert view.totals["due"].text() == format_rupees(425000)
    assert shown.errors == [] and _stock(conn, s) == [0, 0, 0, 0]
    conn.close()


def test_a_bad_amount_is_explained_and_nothing_is_worked_out(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s)
    view.txt_discount.setText("abc")
    assert "Discount" in view.lbl_calc.text() and view.totals["total"].text() == ""
    conn.close()


def test_confirm_adds_stock_and_a_failed_label_print_is_never_called_printed(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s)
    view._open_review = lambda summary, head: "confirm"
    view.review_and_save()
    assert _stock(conn, s) == [4, 0, 0, 6]
    assert any("Purchase confirmed" in i and "PUR/" in i for i in shown.infos)
    assert any("Print barcode labels for the 10 pieces" in a for a in shown.asks)
    assert any("not connected" in e for e in shown.errors)          # print_labels is still a stub
    assert not any("printed" in i.lower() for i in shown.infos)
    assert view.tbl_lines.rowCount() == 0 and view.txt_invoice.text() == ""  # form cleared after a real save
    conn.close()


def test_draft_keeps_stock_and_says_so_then_confirm_from_the_list(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s)
    view._open_review = lambda summary, head: "draft"
    view.review_and_save()
    assert _stock(conn, s) == [0, 0, 0, 0]
    assert any("NOT changed" in i for i in shown.infos)
    view.reload_purchases()
    assert view._purchases[0]["status"] == "draft"
    view.tbl_purchases.setCurrentCell(0, 0)
    assert view.btn_confirm_draft.isEnabled()
    shown.answer = True
    view.confirm_selected_draft()
    assert _stock(conn, s) == [4, 0, 0, 6]
    conn.close()


def test_double_confirm_of_one_draft_adds_stock_once(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s)
    view._open_review = lambda summary, head: "draft"
    view.review_and_save()
    view.tbl_purchases.setCurrentCell(0, 0)
    stale = dict(view._selected_purchase())
    view.confirm_selected_draft()
    view._purchases = [stale]          # a second click on the old, stale row
    view.tbl_purchases.setCurrentCell(0, 0)
    shown.infos.clear()
    view.confirm_selected_draft()
    assert _stock(conn, s) == [4, 0, 0, 6]
    assert shown.infos == [] or all("confirmed" not in i for i in shown.infos)
    conn.close()


def test_missing_details_show_a_short_message_and_save_nothing(s):
    conn, view, shown = _open(s)
    view.review_and_save()
    assert shown.errors == ["Please choose a supplier."]
    view.cmb_supplier.setCurrentIndex(view.cmb_supplier.findData(s.supplier))
    view.txt_invoice.setText("A-1")
    view.review_and_save()
    assert shown.errors[-1] == "Add at least one product with sizes and colours."
    assert shown.infos == []
    conn.close()


def test_a_fault_shows_no_technical_detail_and_keeps_the_form(s, monkeypatch):
    conn, view, shown = _open(s)
    _fill_invoice(view, s)

    def boom(*a, **k):
        raise sqlite3.OperationalError("no such table: secret_table")

    monkeypatch.setattr(svc, "receive_purchase", boom)
    view._open_review = lambda summary, head: "confirm"
    view.review_and_save()
    assert shown.errors == [svc.GENERIC_ERROR] and shown.infos == []
    assert "secret" not in " ".join(shown.errors)
    assert view.tbl_lines.rowCount() == 2 and view._busy is False and view.btn_review.isEnabled()
    conn.close()


def test_same_invoice_number_warns_and_the_user_can_stop(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s, "DUP-1")
    view._open_review = lambda summary, head: "confirm"
    view.review_and_save()
    _fill_invoice(view, s, "DUP-1")
    view._check_duplicate()
    assert "already entered" in view.lbl_duplicate.text()
    shown.answer = False
    shown.asks.clear()
    view.review_and_save()
    assert any("already entered" in a for a in shown.asks)
    assert _stock(conn, s) == [4, 0, 0, 6]  # the second one was not saved
    conn.close()


def test_cancel_a_draft_says_stock_was_not_changed(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s)
    view._open_review = lambda summary, head: "draft"
    view.review_and_save()
    view.tbl_purchases.setCurrentCell(0, 0)
    view.cancel_selected_draft()
    assert any("Stock was not changed" in i for i in shown.infos)
    assert view._purchases[0]["status"] == "cancelled" and not view.btn_confirm_draft.isEnabled()
    assert _stock(conn, s) == [0, 0, 0, 0]
    conn.close()


def test_edit_a_draft_replaces_it(s):
    conn, view, shown = _open(s)
    _fill_invoice(view, s, "ED-1")
    view._open_review = lambda summary, head: "draft"
    view.review_and_save()
    view.tbl_purchases.setCurrentCell(0, 0)
    view.edit_selected_draft()
    assert view.txt_invoice.text() == "ED-1" and len(view._lines) == 2 and view._editing_id
    view._lines.pop()
    view._recalc()
    view.review_and_save()
    rows = svc.list_purchases(conn, view.user_id)
    assert sorted(r["status"] for r in rows) == ["cancelled", "draft"]
    assert [r["qty"] for r in rows if r["status"] == "draft"] == [4]
    conn.close()


# ---------------------------------------------------------------- paying the supplier

def _confirmed_purchase(view, s):
    _fill_invoice(view, s, "PAY-1")
    view._open_review = lambda summary, head: "confirm"
    view.review_and_save()


def test_owner_pays_a_bill_and_the_balance_falls(s):
    conn, view, shown = _open(s)
    _confirmed_purchase(view, s)
    view.cmb_pay_supplier.setCurrentIndex(view.cmb_pay_supplier.findData(s.supplier))
    assert view.lbl_balance.text() == "We owe this supplier: " + format_rupees(525000)
    view.tbl_bills.setCurrentCell(0, 0)
    assert view.txt_pay_amount.text() == format_rupees(525000)
    view.txt_pay_amount.setText("2000")
    shown.infos.clear()
    view.record_payment()
    assert any("was recorded" in i for i in shown.infos)
    assert view.lbl_balance.text() == "We owe this supplier: " + format_rupees(325000)
    assert view.txt_pay_amount.text() == ""
    conn.close()


def test_a_failed_payment_says_nothing_was_recorded(s):
    conn, view, shown = _open(s)
    _confirmed_purchase(view, s)
    view.cmb_pay_supplier.setCurrentIndex(view.cmb_pay_supplier.findData(s.supplier))
    view.tbl_bills.setCurrentCell(0, 0)
    view.txt_pay_amount.setText("999999")
    shown.infos.clear()
    view.record_payment()
    assert shown.infos == [] and any("left to pay" in e and "₹" in e for e in shown.errors)
    view.txt_pay_amount.setText("12.345")
    view.record_payment()
    assert "2 decimal" in shown.errors[-1]
    conn.close()


def test_the_storekeeper_cannot_pay_even_if_the_buttons_were_forced(s):
    conn, view, shown = _open(s, "store")
    assert view.can_pay is False and not view.btn_pay.isEnabled() and view.lbl_pay_note.text()
    _confirmed_purchase(view, s)
    view.cmb_pay_supplier.setCurrentIndex(view.cmb_pay_supplier.findData(s.supplier))
    view.tbl_bills.setCurrentCell(0, 0)
    view.txt_pay_amount.setText("100")
    shown.infos.clear()
    view.record_payment()  # calling it directly: core still refuses
    assert shown.infos == [] and any("not allowed" in e for e in shown.errors)
    conn.close()


# ---------------------------------------------------------------- the grid and review windows

def test_grid_window_builds_a_box_for_each_existing_size_and_colour(s):
    conn, view, _ = _open(s)
    matrix = svc.product_matrix(conn, view.user_id, s.product)
    dialog = GridDialog(matrix)
    assert len(dialog.boxes) == 4
    dialog.boxes[s.variants[0]].setValue(3)
    dialog.txt_cost.setText("")
    dialog.add_to_invoice()
    assert "cost per piece" in dialog.lbl_error.text() and dialog.lines is None
    dialog.txt_cost.setText("450.50")
    dialog.add_to_invoice()
    assert [(l["qty"], l["unit_cost_paise"]) for l in dialog.lines] == [(3, 45050)]
    conn.close()


def test_review_window_returns_the_choice(s):
    conn, view, _ = _open(s)
    _fill_invoice(view, s)
    dialog = ReviewDialog(view._summary, "head")
    assert dialog.choice is None
    dialog.btn_confirm.click()
    assert dialog.choice == "confirm"
    conn.close()


# ---------------------------------------------------------------- purchase returns

def test_return_goods_to_the_supplier(s):
    conn, buy, _ = _open(s)
    _confirmed_purchase(buy, s)
    conn.close()
    conn, view, shown = _open(s, "store", PurchaseReturnsView)
    assert len(view._purchases) == 1
    view.tbl_purchases.setCurrentCell(0, 0)
    assert len(view._boxes) == 2 and view._boxes[0].maximum() == 4
    view._boxes[0].setValue(3)
    view.return_goods()
    assert shown.errors == ["A reason is required for a purchase return."]
    view.txt_reason.setText("torn")
    view.return_goods()
    assert _stock(conn, s) == [1, 0, 0, 6]
    assert any("Debit note DN/" in i for i in shown.infos)
    assert view.txt_reason.text() == "" and view._boxes[0].value() == 0
    shown.errors.clear()
    view.txt_reason.setText("again")
    view.return_goods()  # a second press with nothing chosen returns nothing
    assert shown.errors == ["Enter how many pieces to return."] and _stock(conn, s) == [1, 0, 0, 6]
    assert view.tbl_history.rowCount() == 1
    conn.close()


def test_return_is_limited_by_what_is_in_stock(s):
    conn, buy, _ = _open(s)
    _confirmed_purchase(buy, s)
    from core.stock import add_movement
    from db.connection import transaction
    with transaction(conn):
        add_movement(conn, s.variants[0], -3, "sale")
    conn.close()
    conn, view, shown = _open(s, "store", PurchaseReturnsView)
    view.tbl_purchases.setCurrentCell(0, 0)
    assert view._boxes[0].maximum() == 1
    conn.close()


def test_a_failed_return_says_nothing_was_returned(s, monkeypatch):
    conn, buy, _ = _open(s)
    _confirmed_purchase(buy, s)
    conn.close()
    conn, view, shown = _open(s, "store", PurchaseReturnsView)
    view.tbl_purchases.setCurrentCell(0, 0)
    view._boxes[0].setValue(2)
    view.txt_reason.setText("faulty")
    monkeypatch.setattr(svc, "return_to_supplier", lambda *a, **k: (_ for _ in ()).throw(sqlite3.DatabaseError("disk")))
    shown.infos.clear()
    view.return_goods()
    assert shown.infos == [] and shown.errors == [svc.GENERIC_ERROR] and _stock(conn, s) == [4, 0, 0, 6]
    assert view._busy is False
    conn.close()