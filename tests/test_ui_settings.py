"""The Settings screen uses the tested core code: it says "saved" only when it really was, keeps what the owner
is typing, and a user without the permission cannot change anything."""

import os
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import shop, tax_admin  # noqa: E402
from core import shop_settings as ss  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.screens.settings_screen import SettingsScreen  # noqa: E402

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run
PASSWORD = "owner-password"
MH = "27AAPFU0939F1ZV"  # a GSTIN with a correct check character (state 27 = Maharashtra)


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(f, "owner", PASSWORD, pin="1234")
    shop.add_user(f, conn, owner, "till", "till-password", "cashier")
    conn.close()
    return f


def _open(folder, username="owner"):
    conn, session = shop.open_shop(folder, username, PASSWORD if username == "owner" else f"{username}-password")
    view = SettingsScreen(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], asks=[], answer=True)
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def ask(title, text):
        shown.asks.append(text)
        return shown.answer

    view._ask = ask
    return conn, view, shown


def _fill_shop(view, **over):
    values = dict(name="Anu Cloth House", address="MG Road, Pune", phone="9876543210", gstin=MH)
    values.update(over)
    view.txt_name.setText(values["name"])
    view.txt_address.setText(values["address"])
    view.txt_phone.setText(values["phone"])
    view.txt_gstin.setText(values["gstin"])


def test_owner_saves_the_shop_details(folder):
    conn, view, shown = _open(folder)
    _fill_shop(view)
    assert view._dirty is True and view.lbl_dirty.text() != ""
    view.save_shop()
    assert shown.errors == [] and len(shown.infos) == 1
    profile = ss.get_shop_profile(conn)
    assert profile["name"] == "Anu Cloth House" and profile["gstin"] == MH and profile["state_code"] == "27"
    assert view._dirty is False and view.lbl_dirty.text() == ""
    assert view.txt_gstin.text() == MH
    conn.close()


def test_a_wrong_gstin_is_refused_with_a_short_message_and_nothing_is_saved(folder):
    conn, view, shown = _open(folder)
    _fill_shop(view, gstin="27AAPFU0939F1ZX")
    view.save_shop()
    assert len(shown.errors) == 1 and "check character" in shown.errors[0] and shown.infos == []
    assert ss.get_shop_profile(conn)["is_set_up"] is False
    assert view._dirty is True  # what was typed is kept so it can be corrected
    conn.close()


def test_saving_twice_changes_nothing_the_second_time(folder):
    conn, view, shown = _open(folder)
    _fill_shop(view)
    view.save_shop()
    view.save_shop()
    assert shown.infos[-1] == "Nothing was changed."
    conn.close()


def test_changing_the_tax_type_asks_first(folder):
    conn, view, shown = _open(folder)
    _fill_shop(view)
    view.save_shop()
    view.cmb_scheme.setCurrentIndex(view.cmb_scheme.findData("composition"))
    shown.answer = False
    view.save_shop()
    assert len(shown.asks) == 1 and ss.get_shop_profile(conn)["tax_scheme"] == "regular"
    shown.answer = True
    view.save_shop()
    assert ss.get_shop_profile(conn)["tax_scheme"] == "composition"
    conn.close()


def test_preferences_are_saved_and_come_back(folder):
    conn, view, shown = _open(folder)
    kind, box = view._pref_widgets["credit_sales_enabled"]
    box.setChecked(True)
    view._pref_widgets["return_window_days"][1].setText("15")
    view.save_preferences()
    prefs = ss.get_preferences(conn)
    assert prefs["credit_sales_enabled"]["value"] is True and prefs["return_window_days"]["value"] == 15
    assert shown.errors == [] and view._pref_widgets["return_window_days"][1].text() == "15"
    conn.close()


def test_a_wrong_preference_changes_nothing_at_all(folder):
    conn, view, shown = _open(folder)
    view._pref_widgets["credit_sales_enabled"][1].setChecked(True)
    view._pref_widgets["auto_lock_minutes"][1].setText("9999")
    view.save_preferences()
    assert len(shown.errors) == 1 and shown.infos == []
    assert ss.get_preferences(conn)["credit_sales_enabled"]["value"] is False  # all or nothing
    conn.close()


def test_language_change_says_it_applies_at_the_next_sign_in(folder):
    conn, view, shown = _open(folder)
    combo = view._pref_widgets["ui_language"][1]
    combo.setCurrentIndex(combo.findData("hi"))
    view.save_preferences()
    assert "next time" in shown.infos[-1]
    conn.close()


def test_undo_goes_back_to_what_was_saved(folder):
    conn, view, shown = _open(folder)
    view._pref_widgets["allow_negative_stock"][1].setChecked(True)
    assert view._dirty is True
    view.undo()
    assert view._pref_widgets["allow_negative_stock"][1].isChecked() is False and view._dirty is False
    conn.close()


def test_opening_the_page_again_keeps_what_is_being_typed(folder):
    conn, view, shown = _open(folder)
    view.txt_name.setText("Half typed")
    view.refresh()
    assert view.txt_name.text() == "Half typed"
    conn.close()


def test_the_gst_rate_table_is_shown(folder):
    conn, view, shown = _open(folder)
    assert view.tbl_tax.rowCount() >= 1
    conn.close()


def test_a_cashier_cannot_save_anything(folder):
    conn, view, shown = _open(folder, "till")
    _fill_shop(view)
    view.save_shop()
    view._pref_widgets["credit_sales_enabled"][1].setChecked(True)
    view.save_preferences()
    assert len(shown.errors) == 2 and shown.infos == []
    assert ss.get_shop_profile(conn)["is_set_up"] is False
    conn.close()


def test_the_main_window_shows_the_real_screen_for_the_owner_only(folder):
    conn, session = shop.open_shop(folder, "owner", PASSWORD)
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert isinstance(window.pages.widget(window.allowed.index("Settings")), SettingsScreen)
    assert "Settings" not in mw.PENDING
    conn.close()
    conn, session = shop.open_shop(folder, "till", "till-password")
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert "Settings" not in window.allowed
    conn.close()


# ---------------------------------------------------------------- GST rate rules and document numbers

TODAY = date.today()


def _rule_row(view, hsn, no_limit):
    return next(i for i, r in enumerate(view._rules)
                if r["hsn_prefix"] == hsn and (r["price_ceiling_paise"] is None) == no_limit)


def test_owner_adds_a_gst_rate_rule(folder):
    conn, view, shown = _open(folder)
    before = view.tbl_tax.rowCount()
    view._rule_dialog = lambda: {"hsn": "64", "rate": "5", "limit": "", "note": "Footwear",
                                 "start": TODAY.strftime("%d/%m/%Y")}
    view.add_rule()
    assert shown.errors == [] and shown.infos == ["GST rate rule added."]
    assert view.tbl_tax.rowCount() == before + 1
    assert any(r["hsn_prefix"] == "64" and r["note"] == "Footwear" for r in tax_admin.list_tax_rules(conn))
    conn.close()


def test_a_wrong_rule_is_refused_and_nothing_is_added(folder):
    conn, view, shown = _open(folder)
    before = view.tbl_tax.rowCount()
    view._rule_dialog = lambda: {"hsn": "64", "rate": "abc", "limit": "", "note": "",
                                 "start": TODAY.strftime("%d/%m/%Y")}
    view.add_rule()
    view._rule_dialog = lambda: {"hsn": "64", "rate": "5", "limit": "ten", "note": "", "start": "01/01/2030"}
    view.add_rule()
    view._rule_dialog = lambda: {"hsn": "64", "rate": "5", "limit": "", "note": "", "start": "not a date"}
    view.add_rule()
    assert len(shown.errors) == 3 and shown.infos == [] and view.tbl_tax.rowCount() == before
    conn.close()


def test_a_cancelled_dialog_does_nothing(folder):
    conn, view, shown = _open(folder)
    view._rule_dialog = lambda: None
    view.add_rule()
    view._text_dialog = lambda *a, **k: None
    view.tbl_tax.setCurrentCell(0, 0)
    view.end_rule()
    view.tbl_series.setCurrentCell(0, 0)
    view.change_prefix()
    assert shown.errors == [] and shown.infos == []
    conn.close()


def test_owner_ends_a_rule(folder):
    conn, view, shown = _open(folder)
    row = _rule_row(view, "62", no_limit=True)
    rule_id = view._rules[row]["id"]
    view.tbl_tax.setCurrentCell(row, 0)
    view._text_dialog = lambda *a, **k: TODAY.strftime("%d/%m/%Y")
    view.end_rule()
    assert shown.errors == [] and shown.infos == ["The rule has been ended."]
    assert next(r for r in tax_admin.list_tax_rules(conn) if r["id"] == rule_id)["valid_to"] == TODAY.isoformat()
    conn.close()


def test_ending_needs_a_chosen_rule_and_a_valid_date(folder):
    conn, view, shown = _open(folder)
    view.tbl_tax.setCurrentCell(-1, -1)
    view.end_rule()
    assert shown.errors == ["Choose a rule in the table first."]
    view.tbl_tax.setCurrentCell(0, 0)
    view._text_dialog = lambda *a, **k: "99/99/2030"
    view.end_rule()
    assert len(shown.errors) == 2 and shown.infos == []
    conn.close()


def test_owner_changes_a_document_prefix(folder):
    conn, view, shown = _open(folder)
    assert view.tbl_series.rowCount() == 11
    view.tbl_series.setCurrentCell(0, 0)  # the tax invoice series
    assert view._series[0]["series"] == "INV"
    view._text_dialog = lambda *a, **k: "SHOP"
    view.change_prefix()
    assert shown.errors == [] and "next number" in shown.infos[-1]
    assert view.tbl_series.item(0, 1).text() == "SHOP" and view.tbl_series.item(0, 4).text().startswith("SHOP/")
    conn.close()


def test_a_prefix_used_by_another_series_is_refused(folder):
    conn, view, shown = _open(folder)
    view.tbl_series.setCurrentCell(0, 0)
    view._text_dialog = lambda *a, **k: "BOS"
    view.change_prefix()
    assert len(shown.errors) == 1 and "already uses" in shown.errors[0] and shown.infos == []
    view.tbl_series.setCurrentCell(-1, -1)
    view.change_prefix()
    assert shown.errors[-1] == "Choose a document in the table first."
    conn.close()


def test_a_cashier_cannot_change_rates_or_prefixes(folder):
    conn, view, shown = _open(folder, "till")
    view._rule_dialog = lambda: {"hsn": "64", "rate": "5", "limit": "", "note": "",
                                 "start": TODAY.strftime("%d/%m/%Y")}
    view.add_rule()
    view.tbl_series.setCurrentCell(0, 0)
    view._text_dialog = lambda *a, **k: "SHOP"
    view.change_prefix()
    assert len(shown.errors) == 2 and shown.infos == []
    conn.close()