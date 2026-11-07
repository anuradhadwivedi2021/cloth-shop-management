"""The size-set picker only copies sizes into the box; core.size_sets does the rules (see test_size_sets.py)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from core import auth, size_sets  # noqa: E402
from db.connection import open_db  # noqa: E402
from ui.screens.products_screen import NewProductDialog  # noqa: E402
from ui.size_set_widgets import ManageSizeSetsDialog, SizeSetPicker  # noqa: E402

_APP = QApplication.instance() or QApplication([])


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    yield conn, owner, till
    conn.close()


def test_choosing_a_set_fills_the_sizes_box_and_it_stays_editable(s):
    conn, owner, _ = s
    box = QLineEdit()
    picker = SizeSetPicker(conn, owner, box)
    index = next(i for i in range(picker.combo.count()) if picker.combo.itemData(i) == "Waist 28 to 40")
    picker._chosen(index)
    assert box.text() == "28, 30, 32, 34, 36, 38, 40"
    assert picker.combo.currentIndex() == 0  # goes back to the hint
    box.setText(box.text() + ", 42")
    assert box.text().endswith("42")


def test_new_product_dialog_has_the_picker_for_sizes(s):
    conn, owner, _ = s
    dialog = NewProductDialog(None, conn, owner)
    assert dialog.size_picker.combo.count() == len(size_sets.list_size_sets(conn)) + 1
    dialog.size_picker.combo.activated.emit(1)
    assert dialog.values()["sizes_text"] == "S, M, L, XL, XXL"


def test_manager_adds_changes_and_deletes_sets(s):
    conn, owner, _ = s
    dialog = ManageSizeSetsDialog(conn, owner)
    dialog.name_edit.setText("Shoes UK")
    dialog.sizes_edit.setText("6, 7, 8")
    dialog._add()
    assert "Shoes UK" in [x["name"] for x in size_sets.list_size_sets(conn)]
    assert dialog._current_name() == "Shoes UK"
    dialog.sizes_edit.setText("6, 7, 8, 9")
    dialog._save()
    assert size_sets.sizes_text(conn, "Shoes UK") == "6, 7, 8, 9"
    size_sets.delete_size_set(conn, owner, "Shoes UK")
    dialog._reload()
    assert dialog._current_name() is None


def test_a_cashier_sees_the_sets_but_cannot_change_them(s):
    conn, _, till = s
    dialog = ManageSizeSetsDialog(conn, till)
    assert not dialog.add_btn.isEnabled() and not dialog.save_btn.isEnabled() and not dialog.delete_btn.isEnabled()
    assert dialog.list.count() == len(size_sets.list_size_sets(conn))
