# ui/stock_holds_panel.py
"""Trial and reserved stock: pieces kept aside for a customer for a few days (SRS STK-03).

Shown as a tab of the Stock Count and Adjustments screen. Choose the item, say how many pieces, for how
many days and for whom, and press Hold. Held pieces leave sellable stock, so a bill can never sell them.
When the customer brings them back, or decides to buy, select the line and press "Back to stock"; a piece
that is bought is then billed in the normal way.

This screen only shows things and passes what was typed to core.stock_holds. It never touches the database.
"""

import logging

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget

from core import bill_preview as bp
from core import stock_holds
from core.purchase_service import format_ui_date
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

log = logging.getLogger("clothshop")

TITLE = "Trial and reserved"
KIND_TEXT = {"trial": N_("On trial"), "reserved": N_("Reserved")}
DEFAULT_DAYS = {"trial": 3, "reserved": 7}


class StockHoldsPanel(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self._item = None      # the chosen item (variant_info)
        self._holds = []       # the rows of the table
        self._busy = False
        self.lbl_status = label("", "error")

        self.btn_choose = QPushButton(tr("Choose item"))
        self.lbl_item = label(tr("No item chosen yet."))
        pick = QHBoxLayout()
        pick.addWidget(self.btn_choose)
        pick.addWidget(self.lbl_item, 1)

        self.cmb_kind = QComboBox()
        for key, text in KIND_TEXT.items():
            self.cmb_kind.addItem(tr(text), key)
        self.spin_qty = QSpinBox()
        self.spin_qty.setRange(1, 1)
        self.spin_days = QSpinBox()
        self.spin_days.setRange(1, stock_holds.MAX_HOLD_DAYS)
        self.spin_days.setValue(DEFAULT_DAYS["trial"])
        self.txt_for = QLineEdit()
        self.txt_for.setPlaceholderText(tr("Customer name or phone (optional)"))
        self.btn_hold = QPushButton(tr("Hold these pieces"))
        self.btn_hold.setObjectName("primary")
        self.btn_hold.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.cmb_kind)
        row.addWidget(label(tr("Pieces"), wrap=False))
        row.addWidget(self.spin_qty)
        row.addWidget(label(tr("Days"), wrap=False))
        row.addWidget(self.spin_days)
        row.addWidget(self.txt_for, 1)
        row.addWidget(self.btn_hold)

        self.tbl_holds = make_table([
            tr("Item"), tr("Size"), tr("Colour"), tr("Type"), tr("Pieces out"), tr("For"), tr("Keep until"), tr("Status"),
        ])
        self.btn_release = QPushButton(tr("Back to stock"))
        self.btn_release.setEnabled(False)

        outer = QVBoxLayout(self)
        outer.addWidget(label(tr("Pieces kept aside for a customer leave sellable stock, so they cannot be billed. "
                                 "When they come back, or the customer decides to buy, press Back to stock "
                                 "(a bought piece is then billed as usual)."), "hint"))
        outer.addWidget(self.lbl_status)
        outer.addLayout(pick)
        outer.addLayout(row)
        outer.addWidget(self.tbl_holds, 1)
        outer.addWidget(self.btn_release)

        self.btn_choose.clicked.connect(self.choose_item)
        self.cmb_kind.currentIndexChanged.connect(self._kind_changed)
        self.btn_hold.clicked.connect(self.hold)
        self.btn_release.clicked.connect(self.release)
        self.tbl_holds.itemSelectionChanged.connect(self._selection_changed)
        self.reload_holds()

    # ------------------------------------------------------------ choosing and loading

    def choose_item(self):
        from ui.stock_count_view import ItemDialog  # imported here: that module imports this one

        dialog = ItemDialog(self.conn, self)
        if dialog.exec() == QDialog.Accepted and dialog.variant_id is not None:
            self.set_item(dialog.variant_id)

    def set_item(self, variant_id) -> bool:
        info = bp.variant_info(self.conn, variant_id)
        if info is None:
            self.lbl_status.setText(tr("That product was not found."))
            return False
        self._item = info
        self.lbl_status.setText("")
        sellable = info["sellable"]
        self.lbl_item.setText(tr("{item}   In stock: {n}", item=info["label"], n=sellable))
        self.spin_qty.setRange(1, max(sellable, 1))
        self.spin_qty.setValue(1)
        self.btn_hold.setEnabled(sellable > 0)
        if sellable <= 0:
            self.lbl_status.setText(tr("{item} has nothing in sellable stock.", item=info["label"]))
        return True

    def _kind_changed(self, *_):
        self.spin_days.setValue(DEFAULT_DAYS[self.cmb_kind.currentData()])

    def reload_holds(self):
        ok, rows = self._run(lambda: stock_holds.list_holds(self.conn, self.user_id), title=tr(TITLE),
                             quiet_to=self.lbl_status)
        self._holds = rows or []
        self.tbl_holds.setRowCount(0)
        for h in self._holds:
            add_row(self.tbl_holds, [
                h["product_name"], h["size"] or "", h["colour"] or "", tr(KIND_TEXT[h["kind"]]), h["open_qty"],
                h["held_for"] or "", format_ui_date(h["hold_until"]),
                tr("Overdue") if h["overdue"] else tr("Out"),
            ], right={4})
        self._selection_changed()

    def _selection_changed(self, *_):
        self.btn_release.setEnabled(self._selected() is not None)

    def _selected(self):
        row = self.tbl_holds.currentRow()
        return self._holds[row] if 0 <= row < len(self._holds) else None

    # ------------------------------------------------------------ actions

    def hold(self):
        if self._busy:
            return
        if self._item is None:
            self._error(tr(TITLE), tr("Choose the item first."))
            return
        self._busy = True
        try:
            ok, _ = self._run(lambda: stock_holds.place_hold(
                self.conn, self.user_id, self._item["variant_id"], self.spin_qty.value(), self.cmb_kind.currentData(),
                self.spin_days.value(), held_for=self.txt_for.text()), title=tr(TITLE))
        finally:
            self._busy = False
        if not ok:
            return
        self.txt_for.clear()
        self._info(tr(TITLE), tr("Held. The pieces are no longer in sellable stock."))
        self.set_item(self._item["variant_id"])
        self.reload_holds()

    def release(self):
        if self._busy:
            return
        chosen = self._selected()
        if chosen is None:
            self._error(tr(TITLE), tr("Select a line first."))
            return
        self._busy = True
        try:
            ok, count = self._run(lambda: stock_holds.release_hold(self.conn, self.user_id, chosen["id"]),
                                  title=tr(TITLE))
        finally:
            self._busy = False
        if not ok:
            return
        self._info(tr(TITLE), tr("{n} piece(s) are back in sellable stock.", n=count))
        if self._item is not None:
            self.set_item(self._item["variant_id"])
        self.reload_holds()
