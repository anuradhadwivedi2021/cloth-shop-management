"""The Expenses screen uses the tested core code: it says "saved" only when it really was, never deletes or edits
an expense (cancel and correct keep the original), and a user without the permission cannot change anything."""

import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import expenses as ec  # noqa: E402
from core import shop  # noqa: E402
from core.audit import verify_chain  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.screens.expenses_screen import ExpensesScreen  # noqa: E402

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run
PASSWORD = "owner-password"
TODAY = date.today()
TODAY_TEXT = TODAY.strftime("%d/%m/%Y")


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(f, "owner", PASSWORD, pin="1234")
    shop.add_user(f, conn, owner, "till", "till-password", "cashier")
    shop.add_user(f, conn, owner, "ca", "ca-password", "accountant")
    conn.close()
    return f


def _open(folder, username="owner"):
    password = PASSWORD if username == "owner" else f"{username}-password"
    conn, session = shop.open_shop(folder, username, password)
    view = ExpensesScreen(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], reasons=[], answer="rent was wrong", file="")
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def text_dialog(title, text, default=""):
        shown.reasons.append(text)
        return shown.answer

    view._text_dialog = text_dialog
    view._file_dialog = lambda: shown.file
    return conn, view, shown


def _fill(view, *, day=TODAY_TEXT, category="Rent", description="Shop rent", amount="12000", mode="cash",
          reference="", notes=""):
    view.txt_date.setText(day)
    view.cmb_category.setCurrentText(category)
    view.txt_description.setText(description)
    view.txt_amount.setText(amount)
    view.cmb_mode.setCurrentIndex(view.cmb_mode.findData(mode))
    view.txt_reference.setText(reference)
    view.txt_notes.setText(notes)


def _all(conn, user_id=1):
    return ec.list_expenses(conn, user_id=user_id)


def _select(view, description):
    view.tbl.setCurrentCell(next(i for i, r in enumerate(view._rows) if r["description"] == description), 0)


def test_owner_saves_an_expense(folder):
    conn, view, shown = _open(folder)
    _fill(view, amount="1,250.50", reference="UPI-77", notes="March", mode="upi", description="Electricity bill",
          category="Electricity")
    view.save_expense()
    assert shown.errors == [] and shown.infos == ["Expense saved."]
    (row,) = _all(conn)
    assert (row["amount_paise"], row["payment_mode"], row["reference"], row["notes"], row["category"]) == (
        125050, "upi", "UPI-77", "March", "Electricity")
    assert tuple(conn.execute("SELECT COUNT(*), SUM(amount_paise) FROM payments WHERE document_type = 'expense'"
                              ).fetchone()) == (1, 125050)
    assert view.tbl.rowCount() == 1 and view.txt_description.text() == "" and view.txt_amount.text() == ""
    assert "₹1,250.50" in view.lbl_total.text()
    assert verify_chain(conn) == (True, None)
    conn.close()


@pytest.mark.parametrize("fields", [
    dict(description="   "), dict(amount="abc"), dict(amount="0"), dict(amount="-5"), dict(day="31/02/2026"),
    dict(category="  "),
])
def test_a_wrong_expense_is_refused_and_what_was_typed_stays(folder, fields):
    conn, view, shown = _open(folder)
    _fill(view, **fields)
    view.save_expense()
    assert len(shown.errors) == 1 and shown.infos == [] and _all(conn) == []
    assert view.txt_amount.text() == fields.get("amount", "12000")
    conn.close()


def test_pressing_save_twice_cannot_add_the_expense_twice(folder):
    conn, view, shown = _open(folder)
    _fill(view)
    view.save_expense()
    view.save_expense()  # the form was cleared after the first save, so the second press is refused
    assert len(_all(conn)) == 1 and len(shown.errors) == 1
    conn.close()


def test_a_new_category_is_offered_afterwards(folder):
    conn, view, shown = _open(folder)
    _fill(view, category="Tea and snacks", description="Staff tea")
    view.save_expense()
    assert "Tea and snacks" in view._categories and view.cmb_filter.findData("Tea and snacks") >= 0
    conn.close()


def test_a_receipt_file_is_checked(folder, tmp_path):
    conn, view, shown = _open(folder)
    receipt = tmp_path / "bill.jpg"
    receipt.write_bytes(b"x" * 100)
    shown.file = str(receipt)
    view.choose_receipt()
    assert view.txt_receipt.text() == str(receipt)
    _fill(view)
    view.save_expense()
    assert _all(conn)[0]["attachment_path"] == str(receipt) and view.txt_receipt.text() == ""

    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    shown.file = str(big)
    view.choose_receipt()
    _fill(view, description="Second")
    view.save_expense()
    assert len(shown.errors) == 1 and "5 MB" in shown.errors[0] and len(_all(conn)) == 1
    conn.close()


def test_filters_and_totals(folder):
    conn, view, shown = _open(folder)
    old = (TODAY.replace(day=1) - timedelta(days=40)).strftime("%d/%m/%Y")
    for kwargs in (dict(description="Rent", amount="10000"),
                   dict(description="Bags", category="Shopping bags", amount="500"),
                   dict(description="Old rent", amount="9000", day=old)):
        _fill(view, **kwargs)
        view.save_expense()
    assert shown.errors == []
    assert view.tbl.rowCount() == 2  # this month only
    assert "₹10,500.00" in view.lbl_total.text()
    view.cmb_filter.setCurrentIndex(view.cmb_filter.findData("Shopping bags"))
    view.reload()
    assert view.tbl.rowCount() == 1 and "₹500.00" in view.lbl_total.text()
    view.cmb_filter.setCurrentIndex(0)
    view.txt_from.setText("")
    view.txt_search.setText("old")
    view.reload()
    assert view.tbl.rowCount() == 1 and view._rows[0]["description"] == "Old rent"
    view.txt_search.setText("")
    view.reload()
    assert view.tbl.rowCount() == 3 and "₹19,500.00" in view.lbl_total.text()
    view.txt_from.setText("not a date")
    view.reload()
    assert view.lbl_status.text() != ""
    conn.close()


def test_totals_can_be_grouped_by_category_day_or_month(folder):
    conn, view, shown = _open(folder)
    for kwargs in (dict(description="Rent", amount="10000"), dict(description="Rent 2", amount="2000"),
                   dict(description="Bags", category="Shopping bags", amount="500")):
        _fill(view, **kwargs)
        view.save_expense()
    totals = {view.tbl_summary.item(i, 0).text(): view.tbl_summary.item(i, 1).text()
              for i in range(view.tbl_summary.rowCount())}
    assert totals == {"Rent": "₹12,000.00", "Shopping bags": "₹500.00"}
    view.cmb_group.setCurrentIndex(view.cmb_group.findData("day"))
    assert view.tbl_summary.rowCount() == 1 and view.tbl_summary.item(0, 0).text() == TODAY_TEXT
    view.cmb_group.setCurrentIndex(view.cmb_group.findData("month"))
    assert view.tbl_summary.item(0, 0).text() == TODAY.strftime("%Y-%m")
    conn.close()


def test_cancelling_adds_a_linked_entry_and_deletes_nothing(folder):
    conn, view, shown = _open(folder)
    _fill(view, description="Typed by mistake", amount="700")
    view.save_expense()
    _select(view, "Typed by mistake")
    view.cancel_selected()
    assert shown.errors == [] and "cancelled" in shown.infos[-1]
    rows = _all(conn)
    assert len(rows) == 2 and sorted(r["status"] for r in rows) == ["reversal", "reversed"]
    assert sum(r["amount_paise"] for r in rows) == 0 and "₹0.00" in view.lbl_total.text()
    assert conn.execute("SELECT reason FROM expenses WHERE reversal_of IS NOT NULL").fetchone()[0] == "rent was wrong"
    assert verify_chain(conn) == (True, None)
    _select(view, "Typed by mistake")
    view.cancel_selected()  # the second time: already cancelled
    assert len(shown.errors) == 1 and len(_all(conn)) == 2
    conn.close()


def test_cancelling_needs_a_chosen_expense_and_a_reason(folder):
    conn, view, shown = _open(folder)
    _fill(view)
    view.save_expense()
    view.tbl.setCurrentCell(-1, -1)
    view.cancel_selected()
    assert shown.errors == ["Choose an expense in the table first."]
    view.tbl.setCurrentCell(0, 0)
    shown.answer = "   "
    view.cancel_selected()
    assert len(shown.errors) == 2 and len(_all(conn)) == 1
    shown.answer = None  # the owner pressed Cancel in the question
    view.cancel_selected()
    assert len(shown.errors) == 2 and len(_all(conn)) == 1
    conn.close()


def test_correcting_cancels_the_wrong_entry_and_adds_the_right_one(folder):
    conn, view, shown = _open(folder)
    _fill(view, description="Shop rent", amount="12000", reference="CHQ1")
    view.save_expense()
    _select(view, "Shop rent")
    view.start_correction()
    assert view.txt_amount.text() == "12000.00" and view.txt_reference.text() == "CHQ1"
    assert view.btn_save.text() == "Save correction"
    view.txt_amount.setText("12500")
    shown.answer = "amount was wrong"
    view.save_expense()
    assert shown.errors == [] and "corrected" in shown.infos[-1]
    rows = _all(conn)
    assert len(rows) == 3 and sum(r["amount_paise"] for r in rows) == 1250000
    assert sorted(r["status"] for r in rows) == ["active", "reversal", "reversed"]
    new = next(r for r in rows if r["status"] == "active")
    assert new["amount_paise"] == 1250000 and new["corrects_id"] is not None and new["reason"] == "amount was wrong"
    assert view.btn_save.text() == "Save expense" and view.txt_amount.text() == ""
    assert verify_chain(conn) == (True, None)
    conn.close()


def test_a_correction_can_be_given_up(folder):
    conn, view, shown = _open(folder)
    _fill(view)
    view.save_expense()
    view.tbl.setCurrentCell(0, 0)
    view.start_correction()
    view.stop_correcting()
    assert view.txt_amount.text() == "" and view.btn_save.text() == "Save expense" and len(_all(conn)) == 1
    shown.answer = None  # cancelled the reason question: nothing is saved and the form stays
    view.tbl.setCurrentCell(0, 0)
    view.start_correction()
    view.save_expense()
    assert len(_all(conn)) == 1 and view.btn_save.text() == "Save correction"
    conn.close()


def test_a_cancelled_entry_cannot_be_corrected(folder):
    conn, view, shown = _open(folder)
    _fill(view, description="Gone")
    view.save_expense()
    _select(view, "Gone")
    view.cancel_selected()
    view.tbl.setCurrentCell(0, 0)  # the newest row is the cancelling entry
    view.start_correction()
    assert shown.errors == ["Only an active expense can be corrected."]
    conn.close()


def test_a_cashier_cannot_save_and_an_accountant_can_only_look(folder):
    conn, view, shown = _open(folder)
    _fill(view)
    view.save_expense()
    conn.close()
    conn, view, shown = _open(folder, "ca")
    assert view.tbl.rowCount() == 1  # reading is allowed with the reports permission
    _fill(view, description="Not allowed")
    view.save_expense()
    assert len(shown.errors) == 1 and shown.infos == [] and len(_all(conn, 3)) == 1
    conn.close()
    conn, view, shown = _open(folder, "till")
    assert view.tbl.rowCount() == 0 and view.lbl_status.text() != ""  # a cashier cannot even read the list
    _fill(view)
    view.save_expense()
    assert len(shown.errors) == 1 and shown.infos == []
    conn.close()


def test_a_database_fault_shows_a_short_message_without_details(folder, monkeypatch):
    import sqlite3

    from ui.screens import expenses_screen

    conn, view, shown = _open(folder)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error at C:\\secret\\shop.db")

    monkeypatch.setattr(expenses_screen.ec, "add_expense", boom)
    _fill(view)
    view.save_expense()
    assert len(shown.errors) == 1 and "secret" not in shown.errors[0] and view._busy is False
    conn.close()


def test_the_main_window_shows_the_real_screen_for_those_who_may_use_it(folder):
    conn, session = shop.open_shop(folder, "owner", PASSWORD)
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert isinstance(window.pages.widget(window.allowed.index("Expenses")), ExpensesScreen)
    assert "Expenses" not in mw.PENDING
    conn.close()
    conn, session = shop.open_shop(folder, "till", "till-password")
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert "Expenses" not in window.allowed
    conn.close()
