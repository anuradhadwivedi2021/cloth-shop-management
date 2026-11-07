"""Product detail screen: shows the truth, says "saved" only after core saved, never does a double-click twice.

The rules themselves are tested in test_product_detail.py; these tests check that the screen follows them.
"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import auth  # noqa: E402
from core.product_detail import get_product_detail  # noqa: E402
from core.product_edit import add_product  # noqa: E402
from core.stock import add_movement  # noqa: E402
from db.connection import open_db, transaction  # noqa: E402
import ui.products_detail as pd  # noqa: E402

_APP = QApplication.instance() or QApplication([])


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    users = {
        "owner": owner,
        "till": auth.create_user(conn, owner, "till", "till-password", "cashier"),
        "store": auth.create_user(conn, owner, "store", "store-password", "stock"),
        "ca": auth.create_user(conn, owner, "ca", "ca-password", "accountant"),
    }
    made = add_product(conn, owner, "Shirt", sizes=["S", "M"], colours=["Blue"], category="Shirts",
                       default_price_paise=105000, default_cost_paise=60000)
    v1, v2 = made["variant_ids"]
    with transaction(conn):
        add_movement(conn, v1, 7, "opening stock")
        add_movement(conn, v2, 3, "opening stock")
    yield SimpleNamespace(conn=conn, pid=made["product_id"], v1=v1, v2=v2, users=users)
    conn.close()


def _view(s, who="owner"):
    view = pd.ProductDetailView(s.conn, SimpleNamespace(user_id=s.users[who]), s.pid)
    view.events = []
    view.changed.connect(lambda: view.events.append("changed"))
    view.now = [100.0]
    view._clock = lambda: view.now[0]   # a fake clock: tests decide when time passes
    return view


def _detail(s):
    return get_product_detail(s.conn, s.users["owner"], s.pid)


def _later(view):
    view.now[0] += 5


def _select(view, *rows):
    view.table.selectRow(rows[0])
    if len(rows) > 1:
        view.table.selectAll()


# ---------------------------------------------------------------- shows the truth

def test_shows_product_fields_prices_and_every_variant(s):
    view = _view(s)
    assert view.title.text() == "Shirt"
    assert view.edits["category"].text() == "Shirts"
    assert view.price_edit.text() == "1050.00" and view.cost_edit.text() == "600.00" and view.mrp_edit.text() == ""
    assert view.stock_label.text() == "In stock: 10" and view.toggle_btn.text() == "Hide product"
    assert view.table.rowCount() == 2
    assert [view.table.item(r, 0).text() for r in range(2)] == ["S", "M"]
    assert [view.table.item(r, 5).text() for r in range(2)] == ["7", "3"]
    assert view.table.item(0, 6).text() == "₹1,050.00"
    assert view.events == []                      # opening the screen changes nothing


def test_read_only_user_sees_but_cannot_change(s):
    view = _view(s, "till")
    assert view.edits["name"].text() == "Shirt" and view.table.rowCount() == 2
    for widget in (view.edits["name"], view.price_edit, view.save_btn, view.add_btn, view.rack_btn,
                   view.hide_btn, view.toggle_btn):
        assert not widget.isEnabled()
    assert view.cost_edit.isHidden() is True      # no 'view cost' permission
    assert view.read_only_hint.isHidden() is False


def test_accountant_sees_cost_but_cannot_edit_and_stock_staff_cannot_change_prices(s):
    ca = _view(s, "ca")
    assert not ca.cost_edit.isHidden() and not ca.cost_edit.isEnabled() and not ca.save_btn.isEnabled()
    store = _view(s, "store")
    assert store.edits["category"].isEnabled() and store.save_btn.isEnabled()
    assert not store.price_edit.isEnabled() and not store.mrp_edit.isEnabled()
    store.edits["category"].setText("Kurta")
    store.save_btn.click()
    assert store.message.text() == "Saved 1 change(s)." and _detail(s)["product"]["category"] == "Kurta"


def test_product_that_cannot_be_opened_shows_an_error(s):
    view = pd.ProductDetailView(s.conn, SimpleNamespace(user_id=s.users["owner"]), 999)
    assert view.message.objectName() == "error" and view.message.text() == "Product not found."
    assert not view.save_btn.isEnabled()


# ---------------------------------------------------------------- saving

def test_save_says_saved_only_after_core_saved_and_tells_the_count(s):
    view = _view(s)
    view.edits["season"].setText("Summer")
    view.price_edit.setText("1,100")
    view.save_btn.click()
    assert view.message.objectName() == "ok" and view.message.text() == "Saved 2 change(s)."
    product = _detail(s)["product"]
    assert product["season"] == "Summer" and product["price_paise"] == 110000
    assert view.price_edit.text() == "1100.00" and view.events == ["changed"]


def test_invalid_input_shows_an_error_and_nothing_is_saved(s):
    view = _view(s)
    before = _detail(s)["product"]
    view.edits["name"].setText("   ")
    view.edits["season"].setText("Summer")
    view.save_btn.click()
    assert view.message.objectName() == "error" and view.message.text() == "Product name is required."
    assert "Saved" not in view.message.text() and view.events == []
    assert _detail(s)["product"] == before
    view.edits["name"].setText("Shirt")
    view.price_edit.setText("12.345")
    view.save_btn.click()
    assert view.message.objectName() == "error" and "2 decimal places" in view.message.text()
    assert _detail(s)["product"] == before


def test_nothing_changed_is_not_called_saved(s):
    view = _view(s)
    view.save_btn.click()
    assert view.message.objectName() != "ok" and "Saved" not in view.message.text()
    assert view.events == []


def test_an_unexpected_failure_shows_a_short_safe_error(s, monkeypatch):
    view = _view(s)

    def boom(*a, **k):
        raise RuntimeError("disk I/O error at /secret/path")

    monkeypatch.setattr(pd, "save_changes", boom)
    view.edits["season"].setText("Summer")
    view.save_btn.click()
    assert view.message.objectName() == "error"
    assert view.message.text() == "Something went wrong. Nothing was changed."
    assert "secret" not in view.message.text() and view.events == []
    assert _detail(s)["product"]["season"] is None


def test_prices_refused_after_details_saved_is_reported_exactly(s, monkeypatch):
    view = _view(s)
    monkeypatch.setattr(pd, "save_changes", lambda *a, **k: {"details": ["season"], "prices": [], "error": "Selling price cannot be above the MRP."})
    view.save_btn.click()
    assert view.message.objectName() == "error"
    assert view.message.text() == "The details were saved, but the prices were not: Selling price cannot be above the MRP."
    assert view.events == ["changed"]


# ---------------------------------------------------------------- add sizes and colours

def test_add_sizes_shows_a_preview_and_a_double_click_adds_once(s):
    view = _view(s)
    view.new_sizes.setText("M, L")
    view.new_colours.setText("Blue, Red")
    assert view.preview_label.text() == "3 new to add  ·  1 already exist and will be skipped"
    view.add_btn.click()
    view.add_btn.click()                                   # the double-click
    assert view.message.objectName() == "ok"
    assert view.message.text() == "3 new size/colour combination(s) added. 1 already existed."
    assert view.table.rowCount() == 5 and view.events == ["changed"]
    assert len(_detail(s)["variants"]) == 5
    assert view.new_sizes.text() == "" and view.preview_label.text() == ""


def test_adding_what_exists_or_nothing_is_an_error_not_a_success(s):
    view = _view(s)
    view.new_sizes.setText("S")
    view.new_colours.setText("blue")
    view.add_btn.click()
    assert view.message.objectName() == "error" and view.message.text() == "All of these sizes and colours already exist."
    assert view.events == [] and len(_detail(s)["variants"]) == 2
    view.new_sizes.setText("")
    view.new_colours.setText("")
    view.add_btn.click()
    assert view.message.objectName() == "error" and "Enter at least one size and one colour" in view.message.text()


# ---------------------------------------------------------------- hide / show

def test_hiding_a_product_with_stock_asks_first(s, monkeypatch):
    view = _view(s)
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda text: asked.append(text) or False)
    view.toggle_btn.click()
    assert asked == ["10 pieces are still in stock. Hide this product anyway?"]
    assert _detail(s)["product"]["is_active"] is True and view.events == []

    monkeypatch.setattr(view, "_confirm", lambda text: True)
    view.toggle_btn.click()
    view.toggle_btn.click()                                # double-click: must not switch it back on
    assert _detail(s)["product"]["is_active"] is False and view.events == ["changed"]
    assert view.message.text() == "The product is hidden from selling." and view.toggle_btn.text() == "Show product"
    assert view.state_label.text() == "Hidden from selling"

    _later(view)
    view.toggle_btn.click()
    assert _detail(s)["product"]["is_active"] is True and view.message.text() == "The product is active again."


def test_hiding_a_product_without_stock_does_not_ask(s, monkeypatch):
    other = add_product(s.conn, s.users["owner"], "Empty", sizes=["M"], colours=["Red"])["product_id"]
    view = pd.ProductDetailView(s.conn, SimpleNamespace(user_id=s.users["owner"]), other)
    monkeypatch.setattr(view, "_confirm", lambda text: pytest.fail("must not ask when no stock is left"))
    view.toggle_btn.click()
    assert view.message.objectName() == "ok"


def test_hide_and_show_selected_sizes_with_a_stock_warning(s, monkeypatch):
    view = _view(s)
    view.hide_btn.click()
    assert view.message.objectName() == "error" and view.message.text() == "First select one or more sizes in the table."
    asked = []
    monkeypatch.setattr(view, "_confirm", lambda text: asked.append(text) or True)
    _select(view, 1)                                       # M, 3 in stock
    view.hide_btn.click()
    assert asked == ["3 pieces are still in stock for these sizes. Hide them anyway?"]
    assert view.message.text() == "1 size(s) hidden." and view.table.item(1, 7).text() == "Hidden"
    assert [v["is_active"] for v in _detail(s)["variants"]] == [True, False]
    _later(view)
    _select(view, 1)
    view.show_btn.click()
    assert view.message.text() == "1 size(s) shown again." and view.table.item(1, 7).text() == "Active"


def test_declining_the_stock_warning_changes_nothing(s, monkeypatch):
    view = _view(s)
    monkeypatch.setattr(view, "_confirm", lambda text: False)
    _select(view, 0, 1)
    view.hide_btn.click()
    assert [v["is_active"] for v in _detail(s)["variants"]] == [True, True] and view.events == []


# ---------------------------------------------------------------- rack for many

def test_rack_is_set_for_all_selected_and_needs_a_selection(s):
    view = _view(s)
    view.rack_edit.setText("A-3")
    view.rack_btn.click()
    assert view.message.objectName() == "error" and "select" in view.message.text()
    _select(view, 0, 1)
    view.rack_btn.click()
    assert view.message.objectName() == "ok" and view.message.text() == "Rack A-3 set for 2 item(s)."
    assert {v["rack_location"] for v in _detail(s)["variants"]} == {"A-3"}
    assert [view.table.item(r, 4).text() for r in range(2)] == ["A-3", "A-3"]
    _later(view)
    view.rack_edit.setText("")
    _select(view, 0)
    view.rack_btn.click()
    assert view.message.text() == "Rack cleared for 1 item(s)."