"""The Customers screen uses the tested core code: a profile needs the customer's consent, credit dues and
payments are exact, a payment is never deleted (it is reversed), the data rights (copy, erase) work and are
for the owner only, every look at a customer is in the access log, and no technical detail is ever shown."""

import json
import os
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import customer_credit as cc  # noqa: E402
from core import customers as cu  # noqa: E402
from core import shop  # noqa: E402
from core.guarded import sell  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.settings import set_setting  # noqa: E402
from core.stock import add_movement  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.screens import customers_screen  # noqa: E402
from ui.screens.customers_screen import CustomersScreen, birthday_to_core, birthday_to_text  # noqa: E402

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run
PASSWORD = "owner-password"


@pytest.fixture
def s(tmp_path):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", PASSWORD, pin="1234")
    till = shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    shop.add_user(folder, conn, owner, "store", "store-password", "stock")
    shop.add_user(folder, conn, owner, "ca", "ca-password", "accountant")
    ensure_default_tax_rules(conn)
    set_setting(conn, "credit_sales_enabled", 1)
    pid = create_product(conn, "Shirt", hsn="6205", default_price_paise=100000)
    (v,) = generate_variants(conn, pid, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, v, 50, "opening stock")
    oid = owner.user_id
    ravi = cu.create_customer(conn, "9876500001", user_id=oid, name="Ravi Patil", consent_given=True)
    meena = cu.create_customer(conn, "9876500002", user_id=oid, name="Meena Shah", consent_given=True)
    old = cu.create_customer(conn, "9876500003", user_id=oid, name="Old Customer", consent_given=True)
    cu.set_customer_active(conn, old, False, user_id=oid)
    cc.set_credit_terms(conn, ravi, user_id=oid, credit_enabled=True, credit_limit_paise=500000)
    bill = sell(conn, [{"variant_id": v, "qty": 2}], [{"mode": "cash", "amount_paise": 50000}],
                user_id=till, customer_id=ravi)
    conn.close()
    return SimpleNamespace(folder=folder, owner=oid, till=till, ravi=ravi, meena=meena, old=old, bill=bill, v=v)


def _open(s, username="owner"):
    password = PASSWORD if username == "owner" else f"{username}-password"
    conn, session = shop.open_shop(s.folder, username, password)
    view = CustomersScreen(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], asks=[], answers=None, answer=True, text="by mistake", path=None)
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def ask(title, text):
        shown.asks.append(text)
        if shown.answers is not None:
            return shown.answers.pop(0)
        return shown.answer

    view._ask = ask
    view._ask_save_path = lambda name: shown.path
    view.credit._error, view.credit._info, view.credit._ask = view._error, view._info, ask
    view.credit._ask_text = lambda title, text: shown.text
    return conn, view, shown


def _select(view, customer_id):
    view.reload_customers(select_id=customer_id)
    assert view._customer is not None and view._customer["id"] == customer_id


def _names(view):
    return [r["name"] for r in view._rows]


# ---------------------------------------------------------------- date helpers

def test_birthday_is_typed_as_dd_mm_yyyy_and_stored_for_core():
    assert birthday_to_core("25/03/2001") == "2001-03-25"
    assert birthday_to_core("25/03") == "03-25"
    assert birthday_to_core("") == ""
    assert birthday_to_text("2001-03-25") == "25/03/2001" and birthday_to_text("03-25") == "25/03"
    assert birthday_to_text(None) == ""


# ---------------------------------------------------------------- list and search

def test_the_list_hides_archived_customers_until_asked(s):
    conn, view, shown = _open(s)
    assert sorted(_names(view)) == ["Meena Shah", "Ravi Patil"]
    view.chk_archived.setChecked(True)
    assert "Old Customer" in _names(view)


def test_search_finds_by_name_or_part_of_the_phone(s):
    conn, view, shown = _open(s)
    view.txt_search.setText("ravi")
    assert _names(view) == ["Ravi Patil"]
    view.txt_search.setText("0002")
    assert _names(view) == ["Meena Shah"]


def test_opening_a_profile_is_written_to_the_access_log(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    assert view.edit_name.text() == "Ravi Patil" and view.edit_phone.text() == "9876500001"
    entries = cu.list_access_log(conn, s.ravi, user_id=s.owner)
    assert [e["action"] for e in entries] == ["view"] and entries[0]["user"] == "owner"


# ---------------------------------------------------------------- new customer and consent

def test_a_new_customer_needs_consent(s):
    conn, view, shown = _open(s)
    assert view.add_customer("9811111111", name="Asha", consent=False) is False
    assert shown.errors and "consent" in shown.errors[-1].lower()
    assert "Asha" not in [r["name"] for r in cu.search_customers(conn, "", user_id=s.owner)]


def test_a_new_customer_with_consent_is_saved_and_shown(s):
    conn, view, shown = _open(s)
    assert view.add_customer("98111 11111", name="Asha", birthday="25/03/2001", preferred_size="M", consent=True) is True
    assert shown.errors == [] and shown.infos
    assert view._customer["name"] == "Asha" and view._customer["phone"] == "9811111111"
    assert view._customer["birthday"] == "2001-03-25" and view.edit_birthday.text() == "25/03/2001"
    assert view._customer["consent_given"] == 1 and "Consent given" in view.lbl_consent.text()


def test_a_wrong_phone_number_or_birthday_saves_nothing(s):
    conn, view, shown = _open(s)
    assert view.add_customer("12345", name="X", consent=True) is False and "10-digit" in shown.errors[-1]
    assert view.add_customer("9822222222", birthday="31/02/2001", consent=True) is False
    assert len(shown.errors) == 2 and len(view._rows) == 2


def test_a_duplicate_phone_can_open_the_existing_profile(s):
    conn, view, shown = _open(s)
    shown.answers = [True]                                               # "open that profile instead?" yes
    assert view.add_customer("9876500002", name="Someone", consent=True) is False
    assert view._customer["id"] == s.meena and len(view._rows) == 2


def test_a_duplicate_phone_can_be_saved_as_a_different_person_or_dropped(s):
    conn, view, shown = _open(s)
    shown.answers = [False, False]                                       # do not open, do not save anyway
    assert view.add_customer("9876500002", name="Twin", consent=True) is False
    assert len(view._rows) == 2
    shown.answers = [False, True]                                        # save as a different person
    assert view.add_customer("9876500002", name="Twin", consent=True) is True
    assert view._customer["name"] == "Twin" and len(view._rows) == 3


def test_a_user_without_the_permission_cannot_add_a_customer(s):
    conn, view, shown = _open(s, "ca")                                   # the accountant may only look
    assert not view.btn_new.isEnabled()
    assert view.add_customer("9833333333", name="No", consent=True) is False
    assert shown.errors and len(view._rows) == 2


# ---------------------------------------------------------------- editing

def test_saving_changes_says_what_changed_and_nothing_when_unchanged(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    view.edit_name.setText("Meena S. Shah")
    view.edit_birthday.setText("05/11")
    view.save_profile()
    assert "name" in shown.infos[-1] and "birthday" in shown.infos[-1]
    assert cu.get_customer(conn, s.meena, user_id=s.owner)["birthday"] == "11-05"
    view.save_profile()
    assert "Nothing was changed" in shown.infos[-1]


def test_a_wrong_gstin_or_a_phone_used_by_another_is_refused(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    view.edit_gstin.setText("27AAAAA0000A1Z5")
    view.save_profile()
    assert shown.errors and "GSTIN" in shown.errors[-1]
    view.edit_gstin.setText("")
    view.edit_phone.setText("9876500001")                                # Ravi's number
    view.save_profile()
    assert len(shown.errors) == 2 and "already exists" in shown.errors[-1]
    assert cu.get_customer(conn, s.meena, user_id=s.owner)["phone"] == "9876500002"


def test_consent_can_be_withdrawn_and_given_again(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    view.withdraw_consent()
    assert view._customer["is_active"] == 0 and view._customer["consent_given"] == 0
    assert "No consent" in view.lbl_consent.text() and not view.btn_withdraw.isEnabled()
    view.record_consent()
    assert view._customer["is_active"] == 1 and view._customer["consent_given"] == 1


def test_declining_the_consent_questions_changes_nothing(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    shown.answer = False
    view.withdraw_consent()
    assert view._customer["consent_given"] == 1 and shown.infos == []


def test_a_customer_can_be_archived_and_brought_back(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    view.toggle_archive()
    assert view._customer["id"] == s.meena and view._customer["is_active"] == 0     # still on screen
    assert view.chk_archived.isChecked() and view.btn_archive.text() == "Bring customer back"
    view.toggle_archive()
    assert view._customer["is_active"] == 1


# ---------------------------------------------------------------- bills and returns

def test_the_history_tab_lists_bills_and_what_is_owed(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    view.detail.setCurrentIndex(2)
    assert view.tbl_bills.rowCount() == 1 and view.tbl_bills.item(0, 5).text() != "₹0.00"
    assert "Owes" in view.lbl_history.text()
    assert "history" in [e["action"] for e in cu.list_access_log(conn, s.ravi, user_id=s.owner)]


# ---------------------------------------------------------------- credit and dues

def test_the_credit_panel_shows_the_terms_the_dues_and_their_age(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    panel = view.credit
    assert panel.chk_credit.isChecked() and panel.txt_limit.text() == "5000.00"
    assert len(panel._bills) == 1 and panel.tbl_bills.rowCount() == 1
    assert "Owes" in panel.lbl_summary.text() and "0–30" in panel.lbl_ageing.text()
    due = panel._bills[0]["due_paise"]
    assert due == cc.customer_outstanding_paise(conn, s.ravi) and due > 0


def test_the_owner_changes_the_credit_terms(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    panel = view.credit
    panel.chk_credit.setChecked(True)
    panel.txt_limit.setText("2,500")
    panel.spin_days.setValue(15)
    panel.save_terms()
    assert shown.errors == [] and "saved" in shown.infos[-1]
    d = cc.customer_dues(conn, s.meena, user_id=s.owner)
    assert d["credit_enabled"] and d["credit_limit_paise"] == 250000 and d["default_credit_days"] == 15


def test_a_cashier_cannot_change_credit_terms(s):
    conn, view, shown = _open(s, "till")
    _select(view, s.meena)
    assert not view.credit.btn_terms.isEnabled() and not view.credit.txt_limit.isEnabled()
    assert view.credit.btn_receive.isEnabled()                           # but may take payments


def test_a_wrong_credit_limit_is_refused(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    view.credit.txt_limit.setText("abc")
    view.credit.save_terms()
    assert shown.errors and not cc.customer_dues(conn, s.meena, user_id=s.owner)["credit_enabled"]


def test_a_part_payment_pays_the_selected_bill_and_gives_a_receipt(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    panel = view.credit
    before = panel._bills[0]["due_paise"]
    panel.tbl_bills.setCurrentCell(0, 0)
    assert panel.txt_amount.text() == f"{before // 100}.{before % 100:02d}"      # the amount is filled in
    panel.txt_amount.setText("300")
    panel.receive_payment()
    assert shown.errors == [] and "RCT/" in shown.infos[-1]
    assert cc.customer_outstanding_paise(conn, s.ravi) == before - 30000
    assert panel.tbl_receipts.rowCount() == 1 and panel.txt_amount.text() == ""
    assert f"₹{(before - 30000) // 100:,}" in view.credit.lbl_summary.text()


def test_paying_more_than_is_owed_or_nothing_is_refused(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    panel = view.credit
    owed = cc.customer_outstanding_paise(conn, s.ravi)
    panel.txt_amount.setText(str(owed // 100 + 1))
    panel.receive_payment()
    assert shown.errors and "owes only" in shown.errors[-1]
    panel.txt_amount.setText("0")
    panel.receive_payment()
    assert len(shown.errors) == 2
    assert cc.customer_outstanding_paise(conn, s.ravi) == owed and panel.tbl_receipts.rowCount() == 0


def test_declining_or_a_second_click_takes_no_payment(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    owed = cc.customer_outstanding_paise(conn, s.ravi)
    panel = view.credit
    panel.txt_amount.setText("100")
    shown.answer = False
    panel.receive_payment()
    shown.answer = True
    panel._busy = True
    panel.receive_payment()
    assert cc.customer_outstanding_paise(conn, s.ravi) == owed and shown.infos == []


def test_a_wrong_receipt_is_reversed_not_deleted(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    panel = view.credit
    owed = cc.customer_outstanding_paise(conn, s.ravi)
    panel.txt_amount.setText("200")
    panel.receive_payment()
    panel.tbl_receipts.setCurrentCell(0, 0)
    shown.text = ""
    panel.reverse_selected()                                              # a reason is required
    assert shown.errors and "reason" in shown.errors[-1].lower()
    shown.text = "typed the wrong amount"
    panel.reverse_selected()
    assert cc.customer_outstanding_paise(conn, s.ravi) == owed
    assert panel.tbl_receipts.rowCount() == 2                             # the original stays, a reversal is added
    statuses = sorted(panel.tbl_receipts.item(i, 5).text() for i in range(2))
    assert statuses == ["Reversal", "Reversed"]
    panel.tbl_receipts.setCurrentCell(0, 0)
    panel.reverse_selected()                                              # nothing more can be reversed
    assert "already reversed" in shown.errors[-1]


def test_the_pay_by_date_can_be_moved_with_a_reason(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    panel = view.credit
    panel.tbl_bills.setCurrentCell(0, 0)
    texts = iter(["31/12/2099", "customer asked for time"])
    panel._ask_text = lambda title, text: next(texts)
    panel.change_due_date()
    assert shown.errors == [] and "changed" in shown.infos[-1]
    assert cc.customer_dues(conn, s.ravi, user_id=s.owner)["bills"][0]["due_date"] == "2099-12-31"
    panel.tbl_bills.setCurrentCell(0, 0)                                  # the list was reloaded: choose the bill again
    panel._ask_text = lambda title, text: "not a date"
    panel.change_due_date()
    assert "valid date" in shown.errors[-1]


def test_the_accountant_can_look_but_not_take_money(s):
    conn, view, shown = _open(s, "ca")
    _select(view, s.ravi)
    panel = view.credit
    assert panel.tbl_bills.rowCount() == 1
    assert not panel.btn_receive.isEnabled() and not panel.btn_reverse.isEnabled() and not panel.btn_terms.isEnabled()


# ---------------------------------------------------------------- who owes money

def test_the_dues_report_lists_who_owes_and_is_logged(s):
    conn, view, shown = _open(s)
    view.show_dues()
    assert shown.errors == [] and view.tbl_dues.rowCount() == 1
    assert view.tbl_dues.item(0, 0).text() == "Ravi Patil" and "1 customer(s) owe" in view.lbl_dues_total.text()
    assert "dues_report" in [e["action"] for e in cu.list_access_log(conn, s.ravi, user_id=s.owner)]
    view.chk_overdue.setChecked(True)
    view.show_dues()
    assert view.tbl_dues.rowCount() == 0                                  # nothing is overdue yet


def test_the_dues_report_opens_the_customer(s):
    conn, view, shown = _open(s)
    view.show_dues()
    view.tbl_dues.setCurrentCell(0, 0)
    view.open_due_customer()
    assert view._customer["id"] == s.ravi and view.tabs.currentIndex() == 0 and view.detail.currentIndex() == 1
    view.tbl_dues.setCurrentCell(-1, -1)
    view.open_due_customer()
    assert "Select a customer" in shown.errors[-1]


# ---------------------------------------------------------------- privacy rights

def test_only_the_owner_gets_the_privacy_tab(s):
    conn, view, shown = _open(s)
    assert view.detail.count() == 4 and view.privacy_index == 3
    conn2, till_view, _ = _open(s, "till")
    assert till_view.detail.count() == 3 and till_view.privacy_index == -1


def test_a_copy_of_the_customers_data_can_be_saved(s, tmp_path):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    shown.path = str(tmp_path / "meena.json")
    view.export_data()
    assert shown.errors == [] and "personal data" in shown.infos[-1]
    data = json.loads((tmp_path / "meena.json").read_text(encoding="utf-8"))
    assert data["customer"]["phone"] == "9876500002"
    assert "export" in [e["action"] for e in cu.list_access_log(conn, s.meena, user_id=s.owner)]
    shown.path = None                                                     # the user closed the file box
    n = len(shown.infos)
    view.export_data()
    assert len(shown.infos) == n


def test_personal_data_is_erased_but_not_while_money_is_owed(s):
    conn, view, shown = _open(s)
    _select(view, s.ravi)
    view.erase_data()                                                     # Ravi still owes money
    assert shown.errors and "still owes" in shown.errors[-1]
    assert cu.get_customer(conn, s.ravi, user_id=s.owner)["phone"] == "9876500001"
    _select(view, s.meena)
    view.cmb_erase.setCurrentIndex(view.cmb_erase.findData("customer_request"))
    view.erase_data()
    assert "erased" in shown.infos[-1]
    assert "Meena Shah" not in _names(view)
    row = conn.execute("SELECT phone, name FROM customers WHERE id = ?", (s.meena,)).fetchone()
    assert row["phone"] == "" and row["name"] == "Erased customer"


def test_declining_the_erase_question_keeps_the_data(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    shown.answer = False
    view.erase_data()
    assert cu.get_customer(conn, s.meena, user_id=s.owner)["phone"] == "9876500002"


def test_the_privacy_tab_shows_who_looked_at_the_customer(s):
    conn, view, shown = _open(s)
    _select(view, s.meena)
    view.detail.setCurrentIndex(view.privacy_index)
    assert view.tbl_access.rowCount() >= 1 and view.tbl_access.item(0, 1).text() == "owner"


# ---------------------------------------------------------------- reminders

def test_birthdays_and_inactive_customers_are_lists_for_the_owner_to_act_on(s):
    conn, view, shown = _open(s)
    today = date.today()
    cu.update_customer(conn, s.meena, user_id=s.owner, birthday=f"{today.month:02d}-{today.day:02d}")
    view.refresh()
    assert view.tbl_birthdays.rowCount() == 1 and view.tbl_birthdays.item(0, 4).text() == "today"
    assert view.tbl_inactive.rowCount() == 0                              # everyone is new in this test shop


# ---------------------------------------------------------------- permissions, faults, main window

def test_a_user_without_customer_permissions_sees_nothing(s):
    conn, view, shown = _open(s, "store")
    assert view._rows == [] and view.lbl_status.text() != ""
    assert not view.btn_new.isEnabled()


def test_a_fault_shows_no_technical_detail(s, monkeypatch):
    conn, view, shown = _open(s)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error at C:\\secret\\shop.db")

    monkeypatch.setattr(customers_screen.cu, "create_customer", boom)
    assert view.add_customer("9844444444", name="Z", consent=True) is False
    assert len(shown.errors) == 1 and "secret" not in shown.errors[0] and "disk" not in shown.errors[0]
    monkeypatch.setattr(customers_screen.cu, "search_customers", boom)
    view.reload_customers()
    assert "secret" not in view.lbl_status.text() and view.lbl_status.text() != ""


def test_the_main_window_shows_the_real_screen(s):
    conn, session = shop.open_shop(s.folder, "owner", PASSWORD)
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=s.folder)
    assert isinstance(window.pages.widget(window.allowed.index("Customers")), CustomersScreen)
    assert "Customers" not in mw.PENDING
    conn.close()