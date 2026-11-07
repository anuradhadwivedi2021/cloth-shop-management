"""The Sales Returns and Exchanges screen uses the tested core code: it shows the exact amounts before
saving, says "saved" only when it really was, asks for the owner's approval when the policy blocks a
return, keeps returned pieces out of sellable stock until they are checked, and shows no technical detail."""

import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from core import shop  # noqa: E402
from core.money import format_rupees  # noqa: E402
from core.customers import create_customer, set_customer_active  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.returns import get_return, get_store_credit_balance, list_returns_for_sale  # noqa: E402
from core.sales import create_sale  # noqa: E402
from core.settings import set_setting  # noqa: E402
from core.stock import add_movement, get_stock  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import sales_returns_view  # noqa: E402
from ui.sales_returns_view import ReturnCustomerDialog, SalesReturnsView  # noqa: E402

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run
PASSWORD = "owner-password"


@pytest.fixture
def s(tmp_path):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", PASSWORD, pin="1234")
    shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    shop.add_user(folder, conn, owner, "store", "store-password", "stock")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000, category="Shirts")
    polo = create_product(conn, "Polo", hsn="6105", default_price_paise=135000, category="Tops")
    vest = create_product(conn, "Vest", hsn="6109", default_price_paise=85000, category="Innerwear")
    (shirt_m,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    (polo_m,) = generate_variants(conn, polo, ["M"], ["Red"])
    (vest_m,) = generate_variants(conn, vest, ["M"], ["White"])
    with transaction(conn):
        for v in (shirt_m, polo_m, vest_m):
            add_movement(conn, v, 10, "opening stock")
    sale = create_sale(conn, [{"variant_id": shirt_m, "qty": 2}],
                       [{"mode": "cash", "amount_paise": 210000}], user_id=owner.user_id)
    invoice = conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (sale,)).fetchone()["invoice_no"]
    conn.close()
    return SimpleNamespace(folder=folder, shirt=shirt_m, polo=polo_m, vest=vest_m, sale=sale, invoice=invoice)


def _open(s, username="owner"):
    password = PASSWORD if username == "owner" else f"{username}-password"
    conn, session = shop.open_shop(s.folder, username, password)
    view = SalesReturnsView(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], asks=[], answer=True, pin="1234", why="Regular customer")
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def ask(title, text):
        shown.asks.append(text)
        return shown.answer

    view._ask = ask
    view._ask_pin = lambda: shown.pin
    view._ask_override_reason = lambda: shown.why
    return conn, view, shown


def _find(view, s):
    view.txt_invoice.setText(s.invoice)
    view.find_bill()


def _pick(view, outcome):
    view.cmb_outcome.setCurrentIndex(view.cmb_outcome.findData(outcome))


# ---------------------------------------------------------------- finding the bill

def test_finding_a_bill_shows_its_lines_and_limits_the_pieces(s):
    conn, view, shown = _open(s)
    _find(view, s)
    assert view.tbl_lines.rowCount() == 1
    assert len(view._boxes) == 1 and view._boxes[0].maximum() == 2     # 2 sold, none returned yet
    assert s.invoice in view.lbl_bill.text()
    assert view.btn_process.isEnabled()


def test_an_unknown_bill_number_says_so(s):
    conn, view, shown = _open(s)
    view.txt_invoice.setText("INV/00/999999")
    view.find_bill()
    assert "No bill" in view.lbl_status.text()
    assert view.tbl_lines.rowCount() == 0 and not view.btn_process.isEnabled()


# ---------------------------------------------------------------- refund

def test_a_refund_is_saved_once_and_the_pieces_wait_for_a_check(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    assert format_rupees(105000) in view.lbl_preview.text()            # shown before anything is saved
    view.process()
    assert shown.errors == [] and len(shown.infos) == 1
    assert "CN/" in shown.infos[0] and "waiting for a check" in shown.infos[0]
    (ret,) = list_returns_for_sale(conn, s.sale)
    assert ret["refund_paise"] == 105000 and ret["outcome"] == "refund"
    assert get_stock(conn, s.shirt) == 8                                # 10 - 2 sold; the returned piece is not sellable
    assert get_stock(conn, s.shirt, "returned_pending") == 1
    # the screen reloaded: one piece left to return, the boxes are back to zero, history shows the note
    assert view._boxes[0].maximum() == 1 and view._boxes[0].value() == 0
    assert view.tbl_history.rowCount() == 1
    assert view.btn_process.isEnabled()


def test_nothing_chosen_or_other_without_a_note_saves_nothing(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view.process()                                                     # no pieces chosen
    assert shown.errors and "at least one" in shown.errors[-1]
    view._boxes[0].setValue(1)
    view.cmb_reason.setCurrentIndex(view.cmb_reason.findData("other"))
    view.process()                                                     # reason Other needs a note
    assert "note" in view.lbl_status.text().lower()
    assert list_returns_for_sale(conn, s.sale) == []
    assert shown.asks == []


def test_declining_the_question_saves_nothing(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    shown.answer = False
    view.process()
    assert list_returns_for_sale(conn, s.sale) == [] and shown.infos == []


def test_a_second_click_while_busy_does_nothing(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    view._busy = True
    view.process()
    assert list_returns_for_sale(conn, s.sale) == [] and shown.asks == []


# ---------------------------------------------------------------- exchange

def test_a_dearer_exchange_shows_what_the_customer_pays(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "exchange")
    assert not view.box_exchange.isHidden()
    assert view.add_replacement(s.polo, 1) is True
    text = view.lbl_preview.text()
    assert format_rupees(135000) in text and format_rupees(30000) in text   # Rs 1,350 and Rs 300 to pay
    assert not view.txt_pay_amount.isHidden() and view.txt_pay_amount.text() == "300.00"
    view.process()
    assert shown.errors == [] and "Replacement bill" in shown.infos[-1]
    (ret,) = list_returns_for_sale(conn, s.sale)
    data = get_return(conn, ret["id"])
    assert data["exchange_invoice_no"] and ret["credit_applied_paise"] == 105000
    assert get_stock(conn, s.polo) == 9                                 # the replacement went out
    assert view._cart == [] and view.tbl_replacement.rowCount() == 0


def test_a_cheaper_exchange_gives_the_difference_back(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "exchange")
    view.add_replacement(s.vest, 1)
    assert format_rupees(20000) in view.lbl_preview.text()
    assert view.txt_pay_amount.isHidden() and not view.cmb_surplus.isHidden()
    view.process()
    (ret,) = list_returns_for_sale(conn, s.sale)
    assert ret["refund_paise"] == 20000 and ret["credit_applied_paise"] == 85000


def test_a_replacement_that_is_out_of_stock_is_refused(s):
    conn, view, shown = _open(s)
    with transaction(conn):
        add_movement(conn, s.vest, -10, "sold out")
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "exchange")
    assert view.add_replacement(s.vest, 1) is False
    assert "out of stock" in view.lbl_status.text() and view._cart == []


def test_an_exchange_without_replacement_items_is_refused(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "exchange")
    view.process()
    assert shown.errors and "replacement" in shown.errors[-1].lower()
    assert list_returns_for_sale(conn, s.sale) == []


def test_store_credit_for_a_walk_in_bill_is_refused(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "store_credit")
    view.process()
    assert shown.errors and "customer" in shown.errors[-1].lower()
    assert list_returns_for_sale(conn, s.sale) == []


# ---------------------------------------------------------------- the shop's return policy

def test_a_blocked_return_needs_the_owner_pin_and_a_reason(s):
    conn, view, shown = _open(s, "till")
    set_setting(conn, "final_sale_categories", "Shirts")
    _find(view, s)
    view._boxes[0].setValue(1)
    view.process()
    assert shown.errors == [] and len(shown.infos) == 1
    (ret,) = list_returns_for_sale(conn, s.sale)
    assert ret["override_reason"] == "Regular customer" and ret["override_user_id"] == 1   # the owner approved


def test_declining_the_owner_approval_saves_nothing(s):
    conn, view, shown = _open(s, "till")
    set_setting(conn, "final_sale_categories", "Shirts")
    _find(view, s)
    view._boxes[0].setValue(1)
    answers = iter([True, False])               # confirm saving, then refuse the override question
    view._ask = lambda title, text: next(answers)
    view.process()
    assert list_returns_for_sale(conn, s.sale) == [] and shown.infos == []


def test_a_wrong_owner_pin_is_refused_with_a_short_message(s):
    conn, view, shown = _open(s, "till")
    set_setting(conn, "final_sale_categories", "Shirts")
    _find(view, s)
    view._boxes[0].setValue(1)
    shown.pin = "0000"
    view.process()
    assert list_returns_for_sale(conn, s.sale) == []
    assert shown.errors and "PIN" in shown.errors[-1]


# ---------------------------------------------------------------- permissions and faults

def test_a_user_who_may_not_process_returns_is_refused(s):
    conn, view, shown = _open(s, "store")
    _find(view, s)
    view._boxes[0].setValue(1)
    view.process()
    assert shown.errors and list_returns_for_sale(conn, s.sale) == []


def test_a_fault_shows_no_technical_detail_and_saves_nothing(s, monkeypatch):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error at C:\\secret\\shop.db")

    monkeypatch.setattr(sales_returns_view.guarded, "return_items", boom)
    view.process()
    assert shown.infos == [] and len(shown.errors) == 1
    assert "secret" not in shown.errors[0] and "disk" not in shown.errors[0]
    assert view.btn_process.isEnabled()                                 # the user can try again


# ---------------------------------------------------------------- returned items to check

def _return_one(view, s):
    _find(view, s)
    view._boxes[0].setValue(2)
    view.process()


def test_returned_pieces_become_sellable_only_after_the_check(s):
    conn, view, shown = _open(s)
    _return_one(view, s)
    assert get_stock(conn, s.shirt) == 8 and get_stock(conn, s.shirt, "returned_pending") == 2
    assert view.tbl_pending.rowCount() == 1 and view.spin_review.maximum() == 2
    view.spin_review.setValue(1)
    view.review_selected(True)                                          # one piece is fine
    assert get_stock(conn, s.shirt) == 9 and get_stock(conn, s.shirt, "returned_pending") == 1
    view.review_selected(False)                                         # the other is damaged
    assert get_stock(conn, s.shirt, "damaged") == 1 and get_stock(conn, s.shirt, "returned_pending") == 0
    assert view.tbl_pending.rowCount() == 0 and not view.btn_fit.isEnabled()


def test_a_cashier_does_not_get_the_check_tab(s):
    conn, view, shown = _open(s, "till")
    assert view.tabs.count() == 1 and view.can_review is False
    conn2, owner_view, _ = _open(s)
    assert owner_view.tabs.count() == 2


def test_storekeeper_sees_the_waiting_pieces_and_can_check_them(s):
    conn, view, shown = _open(s)
    _return_one(view, s)
    conn2, store_view, store_shown = _open(s, "store")
    assert store_view.tbl_pending.rowCount() == 1
    store_view.review_selected(True)
    assert store_shown.errors == [] and get_stock(conn2, s.shirt) == 10


# ---------------------------------------------------------------- who gets the store credit (walk-in bills)

def _customer(conn, view, phone="9876500001", name="Ravi Patil", active=True):
    customer_id = create_customer(conn, phone, user_id=view.user_id, name=name, consent_given=True)
    if not active:
        set_customer_active(conn, customer_id, False, user_id=view.user_id)
    return customer_id


def _bill_of_a_customer(conn, view, customer_id):
    """A bill that belongs to a saved customer (the fixture's own bill is a walk-in bill)."""
    kurta = create_product(conn, "Kurta", hsn="6204", default_price_paise=50000)
    (variant,) = generate_variants(conn, kurta, ["M"], ["Red"])
    with transaction(conn):
        add_movement(conn, variant, 5, "opening stock")
    sale = create_sale(conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 50000}],
                       user_id=view.user_id, customer_id=customer_id)
    invoice = conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (sale,)).fetchone()["invoice_no"]
    return sale, invoice


def _return_row(conn, sale_id):
    return conn.execute("SELECT * FROM sale_returns WHERE sale_id = ?", (sale_id,)).fetchone()


def test_a_walk_in_bill_with_store_credit_needs_a_customer_first(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    assert view.row_credit.isHidden()                                    # a refund needs no customer
    _pick(view, "store_credit")
    assert not view.row_credit.isHidden() and "walk-in" in view.lbl_credit_customer.text()
    assert view.btn_pick_customer.text() == "Choose customer"
    view.process()
    assert shown.errors == ["Choose the customer who gets the store credit."]
    assert shown.asks == [] and list_returns_for_sale(conn, s.sale) == []


def test_the_store_credit_goes_to_the_chosen_customer(s):
    conn, view, shown = _open(s)
    ravi = _customer(conn, view)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "store_credit")
    view.set_credit_customer(ravi, "Ravi Patil")
    assert "Ravi Patil" in view.lbl_credit_customer.text() and view.btn_pick_customer.text() == "Change customer"
    view.process()
    assert shown.errors == [] and "Ravi Patil" in shown.asks[-1]         # the question names the customer
    assert "Store credit saved for Ravi Patil" in shown.infos[-1]
    assert get_store_credit_balance(conn, ravi) == 105000
    row = _return_row(conn, s.sale)
    assert row["customer_id"] == ravi and row["store_credit_paise"] == 105000
    assert conn.execute("SELECT customer_id FROM sales WHERE id = ?", (s.sale,)).fetchone()[0] is None  # bill stays walk-in
    assert view._credit_customer is None and view.row_credit.isHidden()  # the choice does not carry over


def test_a_refund_never_asks_for_a_customer_or_sends_one(s):
    conn, view, shown = _open(s)
    ravi = _customer(conn, view)
    _find(view, s)
    view._boxes[0].setValue(1)
    view.set_credit_customer(ravi, "Ravi Patil")                         # chosen earlier, then the outcome stays refund
    assert view.row_credit.isHidden()
    view.process()
    assert shown.errors == [] and get_store_credit_balance(conn, ravi) == 0
    assert _return_row(conn, s.sale)["customer_id"] is None


def test_a_cheaper_exchange_paid_back_as_store_credit_needs_a_customer(s):
    conn, view, shown = _open(s)
    ravi = _customer(conn, view)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "exchange")
    view.add_replacement(s.vest, 1)                                      # Rs 850 for a Rs 1,050 shirt: Rs 200 back
    assert view.row_credit.isHidden()                                    # paid back in cash by default
    view.cmb_surplus.setCurrentIndex(view.cmb_surplus.findData("store_credit"))
    assert not view.row_credit.isHidden()
    view.process()
    assert "Choose the customer" in shown.errors[-1] and list_returns_for_sale(conn, s.sale) == []
    view.set_credit_customer(ravi, "Ravi Patil")
    view.process()
    assert len(shown.errors) == 1 and get_store_credit_balance(conn, ravi) == 20000


def test_a_dearer_exchange_never_needs_a_customer(s):
    conn, view, shown = _open(s)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "exchange")
    view.cmb_surplus.setCurrentIndex(view.cmb_surplus.findData("store_credit"))
    view.add_replacement(s.polo, 1)                                      # the customer pays Rs 300 more
    assert view.row_credit.isHidden()
    view.process()
    assert shown.errors == [] and len(list_returns_for_sale(conn, s.sale)) == 1


def test_the_bill_of_a_saved_customer_needs_no_choice(s):
    conn, view, shown = _open(s)
    ravi = _customer(conn, view)
    sale, invoice = _bill_of_a_customer(conn, view, ravi)
    view.txt_invoice.setText(invoice)
    view.find_bill()
    view._boxes[0].setValue(1)
    _pick(view, "store_credit")
    assert view.row_credit.isHidden()
    view.process()
    assert shown.errors == [] and get_store_credit_balance(conn, ravi) == 50000
    assert "saved for" not in shown.infos[-1]                            # it is the bill's own customer


def test_opening_another_bill_forgets_the_chosen_customer(s):
    conn, view, shown = _open(s)
    ravi = _customer(conn, view)
    _sale, invoice = _bill_of_a_customer(conn, view, ravi)
    _find(view, s)
    view.set_credit_customer(ravi, "Ravi Patil")
    view.txt_invoice.setText(invoice)
    view.find_bill()
    assert view._credit_customer is None
    view.txt_invoice.setText("INV/00/999999")
    view.set_credit_customer(ravi, "Ravi Patil")
    view.find_bill()                                                     # a number that does not exist
    assert view._credit_customer is None


def test_an_archived_customer_cannot_receive_the_credit(s):
    conn, view, shown = _open(s)
    old = _customer(conn, view, "9876500003", "Old Customer", active=False)
    _find(view, s)
    view._boxes[0].setValue(1)
    _pick(view, "store_credit")
    view.set_credit_customer(old, "Old Customer")
    view.process()
    assert shown.errors and "not active" in shown.errors[-1]
    assert list_returns_for_sale(conn, s.sale) == [] and get_store_credit_balance(conn, old) == 0


def test_choosing_a_customer_through_the_dialog(s, monkeypatch):
    conn, view, shown = _open(s)
    ravi = _customer(conn, view)

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            self.customer_id, self.customer_name = ravi, "Ravi Patil"

        def exec(self):
            return QDialog.Accepted

    monkeypatch.setattr(sales_returns_view, "ReturnCustomerDialog", FakeDialog)
    view.choose_credit_customer()
    assert view._credit_customer == (ravi, "Ravi Patil")


def test_the_customer_dialog_lists_only_active_saved_customers(s):
    conn, view, shown = _open(s)
    _customer(conn, view, "9876500001", "Ravi Patil")
    _customer(conn, view, "9876500002", "Meena Shah")
    _customer(conn, view, "9876500003", "Old Customer", active=False)
    dialog = ReturnCustomerDialog(conn, view.user_id)
    assert dialog.results.count() == 2
    dialog.txt_search.setText("meena")
    assert dialog.results.count() == 1
    dialog._pick(dialog.results.item(0))
    assert dialog.customer_name == "Meena Shah" and dialog.customer_id is not None
    dialog.txt_search.setText("nobody")
    assert dialog.results.count() == 0 and "No customer found" in dialog.message.text()


def test_the_customer_dialog_refuses_a_user_who_may_not_view_customers(s):
    conn, session = shop.open_shop(s.folder, "store", "store-password")
    dialog = ReturnCustomerDialog(conn, session.user_id)
    assert dialog.results.count() == 0 and "not allowed" in dialog.message.text()


def test_the_customer_dialog_shows_no_technical_detail(s, monkeypatch):
    conn, view, shown = _open(s)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error at C:\\secret\\shop.db")

    monkeypatch.setattr(sales_returns_view, "search_customers", boom)
    dialog = ReturnCustomerDialog(conn, view.user_id)
    text = dialog.message.text()
    assert text and "secret" not in text and "disk" not in text