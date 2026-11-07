"""The billing counter must save a bill exactly once, block missing stock, and say 'saved' only when it is."""

import os
import string

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from core import shop  # noqa: E402
from core.customers import create_customer  # noqa: E402
from core.money import format_rupees  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.shop_settings import update_shop_profile  # noqa: E402
from core.stock import add_movement, get_stock  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import billing_counter_view as bcv  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui import print_service  # noqa: E402

PASSWORD = "owner-password"
_APP = QApplication.instance() or QApplication([])

_CHARS = string.digits + string.ascii_uppercase


def make_gstin(state, pan="AAPFU0939F", entity="1"):
    body = f"{state}{pan}{entity}Z"
    total = 0
    for position, char in enumerate(body):
        product = _CHARS.index(char) * (2 if position % 2 else 1)
        total += product // 36 + product % 36
    return body + _CHARS[(36 - total % 36) % 36]


MH, KA = make_gstin("27"), make_gstin("29", pan="BBQPK1234L")


def _dispose(widget):
    try:
        widget.setParent(None)
        widget.deleteLater()
    except RuntimeError:  # an earlier cleanup already deleted it
        return
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _APP.processEvents()


@pytest.fixture
def env(tmp_path):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", PASSWORD, pin="1234")
    shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    update_shop_profile(conn, owner.user_id, name="Anu Cloth House",
                        address="MG Road, Pune", phone="9876543210", gstin=MH)
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", brand="Raymond", hsn="6205", default_price_paise=105000)
    generate_variants(conn, shirt, ["M", "L"], ["Blue", "Red"])
    variants = {r["size"] + r["colour"]: r["id"] for r in conn.execute("SELECT * FROM variants WHERE product_id = ?", (shirt,))}
    with transaction(conn):
        add_movement(conn, variants["MBlue"], 10, "opening stock")
        add_movement(conn, variants["LBlue"], 1, "opening stock")
        add_movement(conn, variants["MRed"], 3, "opening stock")
        # LRed has no stock at all
    conn.close()
    return type("Env", (), {"folder": folder, "v": variants, "shirt": shirt})


def _open(env, username="owner"):
    conn, session = shop.open_shop(env.folder, username, PASSWORD if username == "owner" else f"{username}-password")
    view = bcv.BillingCounterView(conn, session, env.folder)
    return conn, session, view


@pytest.fixture
def view(env, monkeypatch):
    printed = []

    def fake_print(conn, session, sale_id, *, parent=None, duplicate=False):
        printed.append(sale_id)
        return print_service.PrintResult(False, "Printing is not connected yet.")

    monkeypatch.setattr(print_service, "print_bill", fake_print)
    conn, session, v = _open(env)
    v.printed = printed
    yield v
    _dispose(v)
    conn.close()


def sales(view):
    return view.conn.execute("SELECT * FROM sales ORDER BY id").fetchall()


def pay_total(view, mode="cash"):
    return [{"mode": mode, "amount_paise": view._preview["total_paise"]}]


# ------------------------------------------------------------------ finding items (scanner as keyboard)

def test_scanned_barcode_adds_the_exact_variant(view, env):
    barcode = view.conn.execute("SELECT barcode FROM variants WHERE id = ?", (env.v["MBlue"],)).fetchone()[0]
    view.scan.setText(barcode)
    view.submit_code(view.scan.text())
    assert view.cart == [{"variant_id": env.v["MBlue"], "qty": 1, "discount_paise": 0}]
    assert view.table.rowCount() == 1 and view.scan.text() == ""
    view.submit_code(barcode)  # scanning again adds one more piece to the same line
    assert view.cart[0]["qty"] == 2 and view.table.rowCount() == 1


def test_name_search_with_one_match_opens_the_size_colour_picker(view, env, monkeypatch):
    asked = {}

    def picker(product_id, name):
        asked["args"] = (product_id, name)
        return env.v["MRed"], 2

    monkeypatch.setattr(view, "_run_picker", picker)
    view.submit_code("Raymond")
    assert asked["args"] == (env.shirt, "Shirt")
    assert view.cart == [{"variant_id": env.v["MRed"], "qty": 2, "discount_paise": 0}]


def test_unknown_text_shows_an_error(view):
    view.submit_code("zzzz-nothing")
    assert view.status.objectName() == "error" and "Nothing found" in view.status.text()
    assert view.cart == []


# ------------------------------------------------------------------ stock check blocks

def test_out_of_stock_variant_is_blocked_and_alternatives_are_shown(view, env):
    assert view.add_variant(env.v["LRed"]) is False
    assert view.cart == []
    text = view.status.text()
    assert view.status.objectName() == "error" and "out of stock" in text
    assert "M Blue (10)" in text  # help the cashier pick an available alternative


def test_cannot_add_more_than_the_stock(view, env):
    assert view.add_variant(env.v["LBlue"]) is True       # only 1 in stock
    assert view.add_variant(env.v["LBlue"]) is False
    assert view.cart[0]["qty"] == 1 and "only 1 in stock" in view.status.text()
    assert view.set_qty(0, 5) is False and view.cart[0]["qty"] == 1


def test_stock_that_disappeared_after_adding_blocks_the_save(view, env):
    view.add_variant(env.v["MBlue"], 5)
    with transaction(view.conn):
        add_movement(view.conn, env.v["MBlue"], -8, "sold elsewhere")  # 2 left
    ok, message = view.save_bill([{"mode": "cash", "amount_paise": 525000}])
    assert ok is False and "only 2 in stock" in message
    assert sales(view) == [] and get_stock(view.conn, env.v["MBlue"]) == 2
    assert view.status.objectName() == "error" and view.printed == []
    assert view.cart[0]["qty"] == 5  # the cart is kept so the cashier can fix it


# ------------------------------------------------------------------ saving exactly once

def test_double_click_on_save_saves_one_bill_and_reduces_stock_once(view, env):
    view.add_variant(env.v["MBlue"], 2)
    dlg = bcv.PaymentDialog(view._preview["total_paise"], view.save_bill, parent=view)
    dlg.save_btn.click()
    dlg.save_btn.click()   # the second click of a double-click
    dlg.submit()           # and a keyboard Enter on top of it
    assert len(sales(view)) == 1
    assert get_stock(view.conn, env.v["MBlue"]) == 8
    assert len(view.conn.execute("SELECT * FROM payments WHERE document_type = 'sale'").fetchall()) == 1
    assert view.printed == [view.last_sale_id]  # the print hook runs once, after saving
    assert view.cart == [] and "saved" in view.status.text() and view.status.objectName() == "ok"


def test_saving_twice_without_new_items_does_not_make_a_second_bill(view, env):
    view.add_variant(env.v["MBlue"])
    payments = pay_total(view)
    assert view.save_bill(payments)[0] is True
    ok, message = view.save_bill(payments)
    assert ok is False and "at least one item" in message
    assert len(sales(view)) == 1 and get_stock(view.conn, env.v["MBlue"]) == 9


def test_retry_after_a_crash_that_happened_after_saving_returns_the_same_bill(view, env, monkeypatch):
    """If the program fails after the database already saved the bill, pressing Save again must not duplicate it."""
    real_sell = bcv.sell

    def sell_then_crash(*args, **kwargs):
        real_sell(*args, **kwargs)
        raise RuntimeError("power cut right after commit")

    view.add_variant(env.v["MBlue"], 2)
    payments = pay_total(view)
    monkeypatch.setattr(bcv, "sell", sell_then_crash)
    ok, message = view.save_bill(payments)
    assert ok is False and view.status.objectName() == "error" and "power cut" not in message  # no internal detail
    monkeypatch.setattr(bcv, "sell", real_sell)
    ok, _ = view.save_bill(payments)
    assert ok is True
    assert len(sales(view)) == 1 and get_stock(view.conn, env.v["MBlue"]) == 8


def test_a_changed_bill_gets_a_new_attempt_key(view, env):
    view.add_variant(env.v["MBlue"])
    first = view._attempt_key
    view._recalc()
    assert view._attempt_key == first  # nothing changed: same attempt
    view.add_variant(env.v["MBlue"])
    assert view._attempt_key != first


# ------------------------------------------------------------------ a failed save is an error, never "saved"

def test_failed_save_shows_an_error_and_nothing_is_saved(view, env, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError("Payment is short by Rs 500.00. Credit sales are switched off; collect the full amount.")

    view.add_variant(env.v["MBlue"])
    monkeypatch.setattr(bcv, "sell", refuse)
    ok, message = view.save_bill(pay_total(view))
    assert ok is False and "short by" in message
    assert view.status.objectName() == "error" and "saved" not in view.status.text().lower()
    assert len(view.cart) == 1 and view.last_sale_id is None and view.printed == []


def test_unexpected_error_shows_a_safe_error_message(view, env, monkeypatch):
    view.add_variant(env.v["MBlue"])
    monkeypatch.setattr(bcv, "sell", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret internal detail")))
    ok, message = view.save_bill(pay_total(view))
    assert ok is False and "secret internal detail" not in message and "could not be saved" in message
    assert view.status.objectName() == "error" and view.printed == []


def test_short_payment_is_refused_by_the_core_and_shown_as_an_error(view, env):
    view.add_variant(env.v["MBlue"])
    ok, message = view.save_bill([{"mode": "cash", "amount_paise": 1000}])
    assert ok is False and "short" in message.lower()
    assert sales(view) == [] and get_stock(view.conn, env.v["MBlue"]) == 10


def test_payment_dialog_shows_the_error_and_stays_open(view, env):
    view.add_variant(env.v["MBlue"])
    dlg = bcv.PaymentDialog(view._preview["total_paise"], view.save_bill, parent=view)
    dlg.rows[0][1].setText("10")
    dlg.submit()
    assert dlg.error.text() and dlg.save_btn.isEnabled() and dlg.result() != QDialog.Accepted
    assert sales(view) == []
    dlg.rows[0][1].setText("not money")
    dlg.submit()
    assert "valid amount" in dlg.error.text()


def test_print_failure_never_undoes_the_sale(view, env):
    view.add_variant(env.v["MBlue"])
    ok, message = view.save_bill(pay_total(view))
    assert ok is True and len(sales(view)) == 1
    assert "NOT printed" in view.status.text() and "saved" in view.status.text()


def test_print_hook_receives_the_saved_sale_id(view, env):
    view.add_variant(env.v["MBlue"])
    view.save_bill(pay_total(view))
    assert view.printed == [sales(view)[0]["id"]]


# ------------------------------------------------------------------ GST / simple bill, place of supply

def test_gst_bill_total_on_screen_equals_the_saved_total(view, env):
    view.add_variant(env.v["MBlue"], 2)
    shown = view.v_total.text()
    view.save_bill(pay_total(view))
    (sale,) = sales(view)
    assert shown == format_rupees(sale["total_paise"]) and sale["tax_mode"] == "regular"
    assert sale["invoice_no"].startswith("INV") and sale["intra_state"] == 1


def test_simple_bill_has_no_gst_and_its_own_series(view, env):
    view.add_variant(env.v["MBlue"], 2)
    view.toggle_mode()
    assert view.table.isColumnHidden(4) and view.v_tax.text() == "not charged"
    view.save_bill(pay_total(view))
    (sale,) = sales(view)
    assert sale["tax_mode"] == "unregistered" and sale["tax_paise"] == 0 and sale["invoice_no"].startswith("BILL")


def test_customer_from_another_state_gets_igst(view, env):
    cid = create_customer(view.conn, "9811111111", user_id=view.user_id, name="Ravi Traders", gstin=KA, consent_given=True)
    view._set_customer(cid, "Ravi Traders")
    assert view._place["intra_state"] is False and view.customer_label.text() == "Customer: Ravi Traders"
    view.add_variant(env.v["MBlue"])
    assert "IGST" in view.tax_caption.text()
    view.save_bill(pay_total(view))
    (sale,) = sales(view)
    assert sale["intra_state"] == 0 and sale["customer_id"] == cid


def test_walk_in_sale_needs_no_customer(view, env):
    view.add_variant(env.v["MBlue"])
    assert view.customer_label.text() == "Walk-in customer"
    assert view.save_bill(pay_total(view))[0] is True
    assert sales(view)[0]["customer_id"] is None


def test_gst_bill_is_blocked_when_the_shop_cannot_issue_one(tmp_path, monkeypatch, env):
    conn, session, v = _open(env)
    from core import shop_settings
    monkeypatch.setattr(bcv, "billing_problems", lambda c, mode: ["The shop GSTIN is not set."] if mode == "regular" else [])
    v.add_variant(env.v["MBlue"])
    assert v.pay_btn.isEnabled() is False and "GSTIN" in v.problem.text()
    ok, message = v.save_bill([{"mode": "cash", "amount_paise": 105000}])
    assert ok is False and "GSTIN" in message and conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    v.toggle_mode()  # a simple bill is still possible
    assert v.pay_btn.isEnabled() is True
    _dispose(v)
    conn.close()


# ------------------------------------------------------------------ discount and owner PIN

def test_discount_within_the_limit_needs_no_pin(env, monkeypatch):
    conn, session, v = _open(env, "till")   # cashier limit is 10%
    monkeypatch.setattr(v, "_ask_pin", lambda *a: pytest.fail("PIN must not be asked"))
    monkeypatch.setattr(print_service, "print_bill", lambda *a, **k: print_service.PrintResult(True, "Printed."))
    v.add_variant(env.v["MBlue"])
    v.discount_edit.setText("10%")
    v._recalc()
    assert v.v_discount.text() == format_rupees(10500)
    assert v.save_bill(pay_total(v))[0] is True
    _dispose(v)
    conn.close()


def test_discount_above_the_limit_needs_the_owner_pin(env, monkeypatch):
    conn, session, v = _open(env, "till")
    monkeypatch.setattr(print_service, "print_bill", lambda *a, **k: print_service.PrintResult(True, "Printed."))
    v.add_variant(env.v["MBlue"])
    v.discount_edit.setText("20%")
    v._recalc()
    payments = pay_total(v)

    monkeypatch.setattr(v, "_ask_pin", lambda *a: None)          # nobody types a PIN
    ok, message = v.save_bill(payments)
    assert ok is False and "PIN" in message and conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0

    monkeypatch.setattr(v, "_ask_pin", lambda *a: "9999")       # wrong PIN
    ok, message = v.save_bill(payments)
    assert ok is False and "Wrong PIN" in message and conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    assert v.status.objectName() == "error"

    monkeypatch.setattr(v, "_ask_pin", lambda *a: "1234")       # the owner's PIN
    ok, _ = v.save_bill(payments)
    assert ok is True and conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'discount_override'").fetchone()[0] == 1
    _dispose(v)
    conn.close()


def test_owner_can_give_a_big_discount_without_pin(view, env, monkeypatch):
    monkeypatch.setattr(view, "_ask_pin", lambda *a: pytest.fail("the owner needs no PIN"))
    view.add_variant(env.v["MBlue"])
    view.discount_edit.setText("50%")
    view._recalc()
    assert view.save_bill(pay_total(view))[0] is True


def test_line_discount_and_bad_discount_text(view, env):
    view.add_variant(env.v["MBlue"], 2)
    assert view.set_line_discount(0, "100") is True and view.cart[0]["discount_paise"] == 10000
    assert view.set_line_discount(0, "99999") is False and "cannot be more" in view.status.text()
    assert view.set_line_discount(0, "abc") is False and view.status.objectName() == "error"
    assert view.cart[0]["discount_paise"] == 10000
    view.discount_edit.setText("200%")
    view._recalc()
    assert view.pay_btn.isEnabled() is False and "percent" in view.problem.text()


# ------------------------------------------------------------------ split payments

def test_split_payment_records_every_part(view, env):
    view.add_variant(env.v["MBlue"], 2)  # 2 x 1050 = 2100 (incl. GST)
    total = view._preview["total_paise"]
    dlg = bcv.PaymentDialog(total, view.save_bill, parent=view)
    dlg.rows[0][1].setText("1000.00")
    dlg.rows[1][0].setCurrentIndex(dlg.rows[1][0].findData("upi"))
    dlg.rows[1][1].setText(f"{(total - 100000) // 100}.{(total - 100000) % 100:02d}")
    dlg.rows[1][2].setText("UPI-REF-1")
    dlg.submit()
    paid = view.conn.execute("SELECT mode, amount_paise, reference FROM payments WHERE document_type = 'sale' ORDER BY id").fetchall()
    assert [(p["mode"], p["amount_paise"]) for p in paid] == [("cash", 100000), ("upi", total - 100000)]
    assert paid[1]["reference"] == "UPI-REF-1"


def test_cash_change_is_shown_and_only_the_net_cash_is_recorded(view, env):
    view.add_variant(env.v["MBlue"])
    total = view._preview["total_paise"]
    dlg = bcv.PaymentDialog(total, view.save_bill, parent=view)
    dlg.rows[0][1].setText("2000")
    assert "Change" in dlg.balance.text() and format_rupees(200000 - total) in dlg.balance.text()
    dlg.submit()
    row = view.conn.execute("SELECT amount_paise FROM payments WHERE document_type = 'sale'").fetchone()
    assert row["amount_paise"] == total


# ------------------------------------------------------------------ hold and resume (stored in the database)

def test_hold_and_resume_a_bill(view, env):
    view.add_variant(env.v["MBlue"], 2)
    view.add_variant(env.v["MRed"], 1)
    assert view.hold_current() is True
    assert view.cart == [] and "slot 1" in view.status.text()
    assert view.held_label.text() == "1 bill(s) on hold"
    assert get_stock(view.conn, env.v["MBlue"]) == 10          # holding takes no stock

    assert view.resume_held(1) is True
    assert [(l["variant_id"], l["qty"]) for l in view.cart] == [(env.v["MBlue"], 2), (env.v["MRed"], 1)]
    assert view.save_bill(pay_total(view))[0] is True
    assert view.conn.execute("SELECT COUNT(*) FROM held_bills").fetchone()[0] == 0  # finished bill leaves the hold list
    assert view.held_label.text() == "0 bill(s) on hold"


def test_at_most_ten_bills_can_be_held(view, env):
    for _ in range(10):
        view.add_variant(env.v["MBlue"])
        assert view.hold_current() is True
    view.add_variant(env.v["MBlue"])
    assert view.hold_current() is False and "10 bills are already on hold" in view.status.text()
    assert len(view.cart) == 1  # the bill is still on the screen


def test_cannot_resume_over_a_bill_in_progress(view, env):
    view.add_variant(env.v["MBlue"])
    view.hold_current()
    view.add_variant(env.v["MRed"])
    assert view.resume_held(1) is False and "Hold or clear" in view.status.text()
    assert [l["variant_id"] for l in view.cart] == [env.v["MRed"]]


def test_resumed_bill_keeps_its_customer_and_discount(view, env):
    cid = create_customer(view.conn, "9822222222", user_id=view.user_id, name="Meera", consent_given=True)
    view._set_customer(cid, "Meera")
    view.add_variant(env.v["MBlue"], 2)
    view.discount_edit.setText("100")
    view._recalc()
    view.hold_current()
    assert view.customer_id is None and view.discount_edit.text() == ""
    view.resume_held(1)
    assert view.customer_id == cid and view.discount_edit.text() == "100.00" and view.held_slot == 1
    assert view.v_discount.text() == format_rupees(10000)


def test_resumed_bill_warns_when_stock_has_gone(view, env):
    view.add_variant(env.v["MBlue"], 6)
    view.hold_current()
    with transaction(view.conn):
        add_movement(view.conn, env.v["MBlue"], -8, "sold elsewhere")
    assert view.resume_held(1) is True
    assert view.status.objectName() == "error" and "only 2 in stock" in view.status.text()
    assert view.save_bill([{"mode": "cash", "amount_paise": 999999}])[0] is False
    assert sales(view) == []


def test_clear_bill(view, env):
    view.add_variant(env.v["MBlue"])
    assert view.clear_bill() is True and view.cart == [] and view.v_total.text() == format_rupees(0)
    assert view.pay_btn.isEnabled() is False


# ------------------------------------------------------------------ keyboard

def test_every_function_key_is_wired(view):
    keys = {sc.key().toString() for sc in view._shortcuts}
    assert {"F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F11", "F12", "Esc", "Del"} <= keys


def test_f4_toggles_the_bill_type_and_delete_removes_a_line(view, env):
    by_key = {sc.key().toString(): sc for sc in view._shortcuts}
    assert view.mode_combo.currentData() == "gst"
    by_key["F4"].activated.emit()
    assert view.mode_combo.currentData() == "simple"
    view.add_variant(env.v["MBlue"])
    view.table.selectRow(0)
    by_key["Del"].activated.emit()
    assert view.cart == []


# ------------------------------------------------------------------ connected in the main window

def test_main_window_opens_the_real_billing_counter(env):
    conn, session = shop.open_shop(env.folder, "till", "till-password")
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House"}, folder=env.folder)
    page = window.pages.widget(window.allowed.index("New Sale / Billing"))
    assert isinstance(page, bcv.BillingCounterView)
    assert not getattr(page, "is_error", False)
    assert "New Sale / Billing" not in mw.PENDING
    _dispose(window)
    conn.close()