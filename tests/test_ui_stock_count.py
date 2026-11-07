"""The Stock Count and Adjustments screen uses the tested core code: nothing changes stock until the owner
approves, a blind count hides the system quantity, a storekeeper can count but not approve, damaged and
missing pieces leave sellable stock with a reason, and no technical detail is ever shown."""

import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import shop  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.sales import create_sale  # noqa: E402
from core.stock import add_movement, get_stock  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import stock_count_view  # noqa: E402
from ui.stock_count_view import NOT_COUNTED, StockCountView  # noqa: E402

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
    vest = create_product(conn, "Vest", hsn="6109", default_price_paise=85000, category="Innerwear")
    shirt_m, shirt_l = generate_variants(conn, shirt, ["M", "L"], ["Blue"])
    (vest_m,) = generate_variants(conn, vest, ["M"], ["White"])
    with transaction(conn):
        for v in (shirt_m, shirt_l, vest_m):
            add_movement(conn, v, 10, "opening stock")
    owner_id = owner.user_id
    conn.close()
    return SimpleNamespace(folder=folder, shirt_m=shirt_m, shirt_l=shirt_l, vest=vest_m, owner_id=owner_id)


def _open(s, username="owner"):
    password = PASSWORD if username == "owner" else f"{username}-password"
    conn, session = shop.open_shop(s.folder, username, password)
    view = StockCountView(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], asks=[], answer=True, text="Customer changed their mind")
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def ask(title, text):
        shown.asks.append(text)
        return shown.answer

    view._ask = ask
    view._ask_text = lambda title, text: shown.text
    return conn, view, shown


def _count(view, variant_id, qty, reason=None):
    """Type a counted quantity (and reason) for one variant on the sheet."""
    i = next(i for i, l in enumerate(view._lines) if l["variant_id"] == variant_id)
    view._spins[i].setValue(qty)
    if reason is not None:
        view._reasons[i].setText(reason)


def _start(view, **kw):
    if kw.get("blind"):
        view.chk_blind.setChecked(True)
    view.start_count()


# ---------------------------------------------------------------- starting and counting

def test_starting_a_count_lists_every_variant_not_yet_counted(s):
    conn, view, shown = _open(s)
    _start(view)
    assert shown.errors == [] and len(shown.infos) == 1
    assert view.tbl_counts.rowCount() == 1 and view.tbl_sheet.rowCount() == 3
    assert all(spin.value() == NOT_COUNTED for spin in view._spins)
    assert view.tbl_sheet.item(0, 4).text() == "10"                      # the system quantity is shown


def test_a_count_can_be_limited_to_one_category(s):
    conn, view, shown = _open(s)
    view.cmb_category.setCurrentIndex(view.cmb_category.findData("Innerwear"))
    _start(view)
    assert view.tbl_sheet.rowCount() == 1 and view._lines[0]["variant_id"] == s.vest


def test_saving_keeps_the_counted_numbers_and_shows_progress(s):
    conn, view, shown = _open(s)
    _start(view)
    _count(view, s.shirt_m, 10)
    _count(view, s.shirt_l, 8)
    view._on_save()
    assert shown.errors == [] and "2 line(s) saved" in shown.infos[-1]
    assert view.tbl_counts.item(0, 4).text() == "2 / 3"
    assert [sp.value() for sp in view._spins].count(NOT_COUNTED) == 1
    assert get_stock(conn, s.shirt_l) == 10                              # counting alone never changes stock


def test_the_difference_is_shown_while_counting(s):
    conn, view, shown = _open(s)
    _start(view)
    _count(view, s.shirt_l, 7)
    i = next(i for i, l in enumerate(view._lines) if l["variant_id"] == s.shirt_l)
    assert view.tbl_sheet.item(i, 6).text() == "-3"


def test_a_blind_count_hides_the_system_quantity(s):
    conn, view, shown = _open(s)
    _start(view, blind=True)
    assert view.tbl_sheet.item(0, 4).text() == "—"
    _count(view, s.shirt_l, 7)
    assert view.tbl_sheet.item(0, 6).text() == "" and "Blind" in view.lbl_hint.text()


# ---------------------------------------------------------------- approving

def test_the_owner_approves_and_stock_changes_by_the_differences_only(s):
    conn, view, shown = _open(s)
    _start(view)
    _count(view, s.shirt_m, 10)
    _count(view, s.shirt_l, 8)
    _count(view, s.vest, 12)
    view._on_review()
    assert view.tbl_diff.rowCount() == 2                                 # the matching line is not listed
    for box in view._diff_reasons:
        box.setText("counted on the shelf")
    view.apply_count()
    assert shown.errors == [] and "approved" in shown.infos[-1]
    assert (get_stock(conn, s.shirt_m), get_stock(conn, s.shirt_l), get_stock(conn, s.vest)) == (10, 8, 12)
    assert view.tbl_counts.item(0, 3).text() == "Applied"
    assert not any(sp.isEnabled() for sp in view._spins) and not view.btn_apply.isEnabled()


def test_approving_without_a_reason_for_a_difference_changes_nothing(s):
    conn, view, shown = _open(s)
    _start(view)
    _count(view, s.shirt_l, 8)
    view.apply_count()
    assert shown.errors and "reason" in shown.errors[-1].lower()
    assert get_stock(conn, s.shirt_l) == 10 and view.tbl_counts.item(0, 3).text() == "Open"


def test_declining_the_question_changes_nothing(s):
    conn, view, shown = _open(s)
    _start(view)
    _count(view, s.shirt_l, 8, "lost")
    shown.answer = False
    view.apply_count()
    assert shown.asks and get_stock(conn, s.shirt_l) == 10 and view.tbl_counts.item(0, 3).text() == "Open"


def test_approving_an_empty_count_is_refused(s):
    conn, view, shown = _open(s)
    _start(view)
    view.apply_count()
    assert shown.errors and "counted" in shown.errors[-1].lower()


def test_a_sale_during_the_count_blocks_the_approval(s):
    conn, view, shown = _open(s)
    _start(view)
    _count(view, s.shirt_l, 8, "lost")
    create_sale(conn, [{"variant_id": s.shirt_l, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                user_id=s.owner_id)
    view.apply_count()
    assert shown.errors and "Stock changed" in shown.errors[-1]
    assert get_stock(conn, s.shirt_l) == 9 and view.tbl_counts.item(0, 3).text() == "Open"


def test_a_storekeeper_counts_and_the_owner_approves(s):
    conn, store, store_shown = _open(s, "store")
    _start(store, blind=True)
    _count(store, s.shirt_l, 8)
    store._on_save()
    assert store_shown.errors == []
    assert not store.btn_apply.isEnabled() and store.can_approve is False
    store.apply_count()                                                  # forced: the core still refuses
    assert store_shown.errors and get_stock(conn, s.shirt_l) == 10
    conn2, owner, shown = _open(s)
    assert owner.tbl_counts.rowCount() == 1
    owner.tbl_counts.setCurrentCell(0, 0)
    assert owner.btn_apply.isEnabled()
    owner._on_review()
    assert owner.tbl_diff.rowCount() == 1                                # the owner sees the differences
    owner._diff_reasons[0].setText("two pieces were sold on credit")
    owner.apply_count()
    assert shown.errors == [] and get_stock(conn2, s.shirt_l) == 8


# ---------------------------------------------------------------- cancelling

def test_cancelling_needs_a_reason_and_leaves_stock_alone(s):
    conn, view, shown = _open(s)
    _start(view)
    _count(view, s.shirt_l, 8, "lost")
    view._on_save()
    shown.text = ""
    view.cancel_count()
    assert shown.errors and "reason" in shown.errors[-1].lower()
    assert view.tbl_counts.item(0, 3).text() == "Open"
    shown.text = "Started by mistake"
    view.cancel_count()
    assert view.tbl_counts.item(0, 3).text() == "Cancelled" and get_stock(conn, s.shirt_l) == 10
    assert not view.btn_save.isEnabled() and not view.btn_cancel.isEnabled()


def test_leaving_the_cancel_question_empty_handed_does_nothing(s):
    conn, view, shown = _open(s)
    _start(view)
    shown.text = None                                                    # the user closed the box
    view.cancel_count()
    assert shown.errors == [] and view.tbl_counts.item(0, 3).text() == "Open"


# ---------------------------------------------------------------- damaged or missing

def test_damaged_pieces_leave_sellable_stock_with_a_reason(s):
    conn, view, shown = _open(s)
    assert view.set_item(s.shirt_m) is True
    view.spin_qty.setValue(2)
    view.txt_adj_reason.setText("torn at the seam")
    view.record_adjustment("damaged")
    assert shown.errors == [] and shown.infos
    assert get_stock(conn, s.shirt_m) == 8 and get_stock(conn, s.shirt_m, "damaged") == 2
    assert view.tbl_adjustments.rowCount() == 1 and view.tbl_adjustments.item(0, 1).text() == "Damaged"
    assert "In stock: 8" in view.lbl_item.text() and view.txt_adj_reason.text() == ""


def test_missing_pieces_are_recorded_and_not_counted_as_stock(s):
    conn, view, shown = _open(s)
    view.set_item(s.vest)
    view.txt_adj_reason.setText("not on the shelf")
    view.record_adjustment("missing")
    assert get_stock(conn, s.vest) == 9 and view.tbl_adjustments.item(0, 1).text() == "Missing"


def test_a_reason_is_required_and_nothing_changes_without_it(s):
    conn, view, shown = _open(s)
    view.set_item(s.shirt_m)
    view.record_adjustment("damaged")
    assert shown.errors and "reason" in shown.errors[-1].lower()
    assert get_stock(conn, s.shirt_m) == 10 and view.tbl_adjustments.rowCount() == 0


def test_marking_without_choosing_an_item_is_refused(s):
    conn, view, shown = _open(s)
    view.record_adjustment("damaged")
    assert shown.errors == ["Choose the item first."]


def test_an_item_with_no_sellable_stock_cannot_be_marked(s):
    conn, view, shown = _open(s)
    with transaction(conn):
        add_movement(conn, s.vest, -10, "sold out")
    view.set_item(s.vest)
    assert not view.btn_damaged.isEnabled() and not view.btn_missing.isEnabled()
    assert "nothing" in view.lbl_status.text().lower()


# ---------------------------------------------------------------- permissions and faults

def test_a_cashier_sees_nothing_and_cannot_start_a_count(s):
    conn, view, shown = _open(s, "till")
    assert view.tbl_counts.rowCount() == 0 and view.lbl_status.text() != ""
    view.start_count()
    assert shown.errors and view.tbl_counts.rowCount() == 0


def test_a_fault_shows_no_technical_detail(s, monkeypatch):
    conn, view, shown = _open(s)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error at C:\\secret\\shop.db")

    monkeypatch.setattr(stock_count_view.sc, "start_count", boom)
    view.start_count()
    assert shown.infos == [] and len(shown.errors) == 1
    assert "secret" not in shown.errors[0] and "disk" not in shown.errors[0]
    assert view.btn_start.isEnabled()