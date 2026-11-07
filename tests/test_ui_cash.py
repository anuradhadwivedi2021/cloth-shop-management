"""The Payments and Cash screen uses the tested core code: the expected cash comes from saved records only, a
difference is shown with its reason and never "fixed", a closed day cannot change, closing never uses old figures,
and only users with the cash permission can act."""

import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import cash, shop  # noqa: E402
from core import expenses as ec  # noqa: E402
from core.audit import verify_chain  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.screens.cash_screen import CashScreen  # noqa: E402

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run
PASSWORD = "owner-password"
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


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
    view = CashScreen(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], asked=[], answer=True)
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def ask(title, text):
        shown.asked.append(text)
        return shown.answer

    view._ask = ask
    return conn, view, shown


def _figure(view, name):
    for row in range(view.tbl_figures.rowCount()):
        if view.tbl_figures.item(row, 0).text() == name:
            return view.tbl_figures.item(row, 1).text()
    return None


def _open_register(view, opening="5000"):
    view.txt_opening.setText(opening)
    view.open_day()


def test_a_fresh_day_is_not_open_and_offers_to_open(folder):
    conn, view, shown = _open(folder)
    assert "Not opened" in view.lbl_day.text() and "not open yet" in view.lbl_note.text()
    assert view.box_open.isHidden() is False and view.box_move.isEnabled() is False
    assert view.box_close.isEnabled() is False and view.txt_opening.text() == "0.00"
    assert _figure(view, "Expected cash in the drawer") == "₹0.00"
    conn.close()


def test_the_owner_opens_the_register_with_opening_cash(folder):
    conn, view, shown = _open(folder)
    _open_register(view)
    assert shown.errors == [] and shown.infos == ["The cash register is open."]
    assert cash.get_cash_day(conn, TODAY, user_id=1)["status"] == "open"
    assert "Open" in view.lbl_day.text() and view.box_open.isHidden()
    assert view.box_move.isEnabled() and view.box_close.isEnabled()
    assert _figure(view, "Opening cash") == "₹5,000.00" and _figure(view, "Expected cash in the drawer") == "₹5,000.00"
    assert view.tbl_history.rowCount() == 1 and verify_chain(conn) == (True, None)
    conn.close()


def test_a_wrong_opening_amount_is_refused_and_a_second_open_too(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "abc")
    _open_register(view, "-5")
    assert len(shown.errors) == 2 and shown.infos == []
    assert cash.get_cash_day(conn, TODAY, user_id=1)["status"] == "not_opened"
    _open_register(view)
    _open_register(view)  # the register is already open
    assert len(shown.errors) == 3 and "already" in shown.errors[-1] and len(shown.infos) == 1
    conn.close()


def test_a_blank_opening_carries_the_last_counted_cash(folder):
    conn, view, shown = _open(folder)
    cash.open_cash_day(conn, YESTERDAY, user_id=1, opening_paise=100000)
    cash.close_cash_day(conn, YESTERDAY, 100000, user_id=1)
    view.reload()
    assert view.txt_opening.text() == "1000.00"
    _open_register(view, "")
    assert shown.errors == [] and _figure(view, "Opening cash") == "₹1,000.00"
    conn.close()


def test_a_day_that_was_never_closed_is_shown_and_must_be_closed_first(folder):
    conn, view, shown = _open(folder)
    cash.open_cash_day(conn, YESTERDAY, user_id=1, opening_paise=0)
    view.reload()
    assert YESTERDAY.strftime("%d/%m/%Y") in view.lbl_day.text() and "Open" in view.lbl_day.text()
    assert "never closed" in view.lbl_warning.text() and view.lbl_warning.isHidden() is False
    assert view.box_move.isEnabled() and view._open_day == YESTERDAY.isoformat()
    conn.close()


def test_cash_put_in_and_taken_out_by_hand(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "1000")
    view.cmb_direction.setCurrentIndex(view.cmb_direction.findData("in"))
    view.txt_move_amount.setText("200")
    view.txt_move_reason.setText("Change from the bank")
    view.save_movement()
    view.cmb_direction.setCurrentIndex(view.cmb_direction.findData("out"))
    view.txt_move_amount.setText("50.50")
    view.txt_move_reason.setText("Tea for staff")
    view.save_movement()
    assert shown.errors == [] and shown.infos[-1] == "The cash movement is saved."
    assert view.tbl_moves.rowCount() == 2 and view.tbl_moves.item(1, 2).text() == "₹50.50"
    assert _figure(view, "+ Cash put in by hand") == "₹200.00" and _figure(view, "- Cash taken out by hand") == "₹50.50"
    assert _figure(view, "Expected cash in the drawer") == "₹1,149.50"
    assert view.txt_move_amount.text() == "" and view.txt_move_reason.text() == ""
    conn.close()


def test_a_wrong_movement_is_refused(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "100")
    view.cmb_direction.setCurrentIndex(view.cmb_direction.findData("out"))
    for amount, reason in (("500", "Too much"), ("0", "Zero"), ("abc", "Bad"), ("10", "   ")):
        view.txt_move_amount.setText(amount)
        view.txt_move_reason.setText(reason)
        view.save_movement()
    assert len(shown.errors) == 4 and "not enough cash" in shown.errors[0] and view.tbl_moves.rowCount() == 0
    assert view.txt_move_amount.text() == "10"  # what was typed stays, so it can be fixed
    conn.close()


def test_the_difference_is_shown_while_typing(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "1000")
    view.txt_counted.setText("1000")
    assert "matches" in view.lbl_difference.text()
    view.txt_counted.setText("990")
    assert "SHORT by ₹10.00" in view.lbl_difference.text()
    view.txt_counted.setText("1010")
    assert "MORE than expected by ₹10.00" in view.lbl_difference.text()
    view.txt_counted.setText("abc")
    assert view.lbl_difference.text() == ""
    conn.close()


def test_closing_with_no_difference(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "1000")
    view.txt_counted.setText("1000")
    view.close_day()
    assert shown.errors == [] and len(shown.asked) == 1
    assert shown.infos[-1] == "The cash day is closed. Expected ₹1,000.00, counted ₹1,000.00, difference ₹0.00."
    day = cash.get_cash_day(conn, TODAY, user_id=1)
    assert day["status"] == "closed" and day["difference_paise"] == 0
    assert "Closed" in view.lbl_day.text() and view.box_move.isEnabled() is False and view.box_close.isEnabled() is False
    assert _figure(view, "Cash counted at closing") == "₹1,000.00"
    assert _figure(view, "Difference (counted - expected)") == "₹0.00"
    assert verify_chain(conn) == (True, None)
    conn.close()


def test_a_difference_needs_a_reason_and_is_never_corrected(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "1000")
    view.txt_counted.setText("990")
    view.close_day()
    assert len(shown.errors) == 1 and "differs" in shown.errors[0] and shown.asked == []
    assert cash.get_cash_day(conn, TODAY, user_id=1)["status"] == "open"
    view.txt_close_reason.setText("Gave too much change")
    view.close_day()
    assert "difference -₹10.00" in shown.infos[-1]
    day = cash.get_cash_day(conn, TODAY, user_id=1)
    assert day["difference_paise"] == -1000 and day["closed_expected_paise"] == 100000 and day["counted_paise"] == 99000
    assert _figure(view, "Reason for the difference") == "Gave too much change"
    assert _figure(view, "Difference (counted - expected)") == "-₹10.00"
    conn.close()


def test_declining_the_question_or_typing_nonsense_closes_nothing(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "1000")
    view.txt_counted.setText("1000")
    shown.answer = False
    view.close_day()
    assert len(shown.asked) == 1 and cash.get_cash_day(conn, TODAY, user_id=1)["status"] == "open"
    view.txt_counted.setText("abc")
    view.close_day()
    assert len(shown.errors) == 1 and len(shown.asked) == 1
    conn.close()


def test_closing_never_uses_figures_that_changed_since_the_page_was_drawn(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "5000")
    ec.add_expense(conn, TODAY, "Rent", "Paid after the page was drawn", 10000, user_id=1, payment_mode="cash")
    view.txt_counted.setText("5000")
    view.close_day()
    assert len(shown.errors) == 1 and "changed since" in shown.errors[0] and shown.asked == []
    assert view._data["expected_paise"] == 490000  # the page was refreshed
    assert cash.get_cash_day(conn, TODAY, user_id=1)["status"] == "open"
    conn.close()


def test_cash_expenses_lower_the_expected_cash_but_other_modes_do_not(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "5000")
    ec.add_expense(conn, TODAY, "Rent", "Shop rent", 120000, user_id=1, payment_mode="cash")
    ec.add_expense(conn, TODAY, "Rent", "Paid by UPI", 90000, user_id=1, payment_mode="upi")
    view.reload()
    assert _figure(view, "- Cash paid for expenses") == "₹1,200.00"
    assert _figure(view, "Expected cash in the drawer") == "₹3,800.00"
    conn.close()


def test_an_earlier_day_can_be_looked_at_and_left_again(folder):
    conn, view, shown = _open(folder)
    cash.open_cash_day(conn, YESTERDAY, user_id=1, opening_paise=70000)
    cash.close_cash_day(conn, YESTERDAY, 70000, user_id=1)
    view.reload()
    assert view.tbl_history.rowCount() == 1 and view.tbl_history.item(0, 1).text() == "Closed"
    assert view.btn_current.isHidden()
    view.show_history_day(0)
    assert YESTERDAY.strftime("%d/%m/%Y") in view.lbl_day.text() and "Closed" in view.lbl_day.text()
    assert _figure(view, "Cash counted at closing") == "₹700.00" and view.btn_current.isHidden() is False
    view.show_current_day()
    assert TODAY.strftime("%d/%m/%Y") in view.lbl_day.text() and view.btn_current.isHidden()
    conn.close()


def test_an_accountant_can_look_but_not_act_and_a_cashier_sees_nothing(folder):
    conn, view, shown = _open(folder)
    _open_register(view, "1000")
    conn.close()
    conn, view, shown = _open(folder, "ca")
    assert "Open" in view.lbl_day.text() and view.lbl_status.text() == ""
    assert "look at the cash register" in view.lbl_note.text()
    assert view.box_move.isEnabled() is False and view.box_close.isEnabled() is False
    view.txt_move_amount.setText("10")
    view.txt_move_reason.setText("x")
    view.save_movement()
    assert len(shown.errors) == 1 and cash.list_cash_movements(conn, TODAY, user_id=3) == []
    conn.close()
    conn, view, shown = _open(folder, "till")
    assert view.lbl_status.text() != "" and view.tbl_history.rowCount() == 0 and view._data is None
    view.save_movement()
    view.close_day()
    assert shown.infos == [] and shown.errors == []  # nothing is loaded, so the buttons do nothing at all
    conn.close()


def test_a_database_fault_shows_a_short_message_without_details(folder, monkeypatch):
    import sqlite3

    from ui.screens import cash_screen

    conn, view, shown = _open(folder)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error at C:\\secret\\shop.db")

    monkeypatch.setattr(cash_screen.cc, "open_cash_day", boom)
    _open_register(view)
    assert len(shown.errors) == 1 and "secret" not in shown.errors[0] and view._busy is False
    conn.close()


def test_the_main_window_shows_the_real_screen_for_those_who_may_use_it(folder):
    conn, session = shop.open_shop(folder, "owner", PASSWORD)
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert isinstance(window.pages.widget(window.allowed.index("Payments and Cash")), CashScreen)
    assert "Payments and Cash" not in mw.PENDING
    conn.close()
    conn, session = shop.open_shop(folder, "till", "till-password")
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert "Payments and Cash" not in window.allowed
    conn.close()
