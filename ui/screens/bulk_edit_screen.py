"""Bulk Edit menu: change season, GST, reorder level or price of many products, with a preview first (PRD-09).

No SQL and no rules here. core.bulk_edit finds the products and reads the typed values; core.product_edit
checks permissions, the MRP ceiling and saves everything in ONE transaction.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from core import bulk_edit as be
from ui.i18n import tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Bulk Edit"


class BulkEditScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._ids = []
        self._changes = None
        self.lbl_status = label("", "error")
        self.lbl_summary = label("")

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search by name, brand, category or code"))
        self.older = QLineEdit()
        self.older.setPlaceholderText(tr("Only stock older than (days)"))
        self.older.setMaximumWidth(220)
        self.btn_search = QPushButton(tr("Search"))
        self.btn_all = QPushButton(tr("Select all shown"))
        self.tbl = make_table([tr("Product"), tr("Category"), tr("Season"), tr("GST %"), tr("Price")])
        self.tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)

        self.season = QLineEdit()
        self.gst = QLineEdit()
        self.reorder = QLineEdit()
        self.price_mode = QComboBox()
        self.price_mode.addItem(tr("Leave the price"), "none")
        self.price_mode.addItem(tr("Set one new price (Rs)"), "set")
        self.price_mode.addItem(tr("Change by percent (+/-)"), "percent")
        self.price = QLineEdit()
        form = QFormLayout()
        form.addRow(tr("Season (blank = leave)"), self.season)
        form.addRow(tr("GST rate % (blank = leave)"), self.gst)
        form.addRow(tr("Reorder level (blank = leave)"), self.reorder)
        form.addRow(tr("Price"), self.price_mode)
        form.addRow("", self.price)
        box = QGroupBox(tr("Change to"))
        box.setLayout(form)

        self.btn_preview = QPushButton(tr("Preview the changes"))
        self.btn_apply = QPushButton(tr("Apply"))
        self.btn_apply.setEnabled(False)
        self.tbl_preview = make_table([tr("Product"), tr("Field"), tr("Old"), tr("New")])

        top = QHBoxLayout()
        for w in (self.search, self.older, self.btn_search, self.btn_all):
            top.addWidget(w)
        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_preview)
        bottom.addWidget(self.btn_apply)
        bottom.addStretch(1)

        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(tr("Bulk Edit"), "title"))
        col.addWidget(label(tr("Select products, choose what to change, and check the preview. Nothing is saved "
                               "until you press Apply. Bills already made keep their old prices."), "subtitle"))
        col.addLayout(top)
        col.addWidget(self.tbl, 2)
        col.addWidget(box)
        col.addLayout(bottom)
        col.addWidget(self.lbl_summary)
        col.addWidget(self.lbl_status)
        col.addWidget(self.tbl_preview, 2)

        self.btn_search.clicked.connect(self.load)
        self.search.returnPressed.connect(self.load)
        self.older.returnPressed.connect(self.load)
        self.btn_all.clicked.connect(self.tbl.selectAll)
        self.btn_preview.clicked.connect(self.do_preview)
        self.btn_apply.clicked.connect(self.do_apply)
        for w in (self.season, self.gst, self.reorder, self.price):
            w.textChanged.connect(self._stale)
        self.price_mode.currentIndexChanged.connect(self._stale)
        self.tbl.itemSelectionChanged.connect(self._stale)
        self.load()

    def _stale(self, *_):
        self._changes = None
        self.btn_apply.setEnabled(False)

    def load(self) -> None:
        self.tbl.setRowCount(0)
        self._rows = []
        ok, rows = self._run(lambda: be.find_products(self.conn, self.user_id, self.search.text(),
                                                      older_than_days=be.parse_days(self.older.text())),
                             title=tr("Search"), quiet_to=self.lbl_status)
        if not ok:
            return
        self._rows = rows
        for r in rows:
            add_row(self.tbl, [r["name"], r["category"], r["season"], r["gst"], r["price"]], right=(3, 4))
        self._stale()

    def _selected_ids(self) -> list:
        return [self._rows[i.row()]["id"] for i in self.tbl.selectionModel().selectedRows()]

    def do_preview(self) -> None:
        self.tbl_preview.setRowCount(0)
        self.lbl_summary.setText("")
        ids = self._selected_ids()

        def work():
            changes = be.build_changes(season=self.season.text(), gst_text=self.gst.text(),
                                       reorder_text=self.reorder.text(), price_mode=self.price_mode.currentData(),
                                       price_text=self.price.text())
            return changes, be.preview(self.conn, self.user_id, ids, changes)

        ok, result = self._run(work, title=tr("Preview the changes"), quiet_to=self.lbl_status)
        if not ok:
            self._stale()
            return
        changes, rows = result
        for r in rows:
            add_row(self.tbl_preview, [r["name"], tr(r["field"]), r["old"], r["new"]], right=(2, 3))
        self._ids, self._changes = ids, changes
        self.btn_apply.setEnabled(bool(rows))
        self.lbl_summary.setText(tr("{n} change(s) to apply.", n=len(rows)) if rows
                                 else tr("Nothing would change for the selected products."))

    def do_apply(self) -> None:
        if not self._changes or not self._ids:
            return
        if not self._ask(tr("Apply"), tr("Apply these changes to {n} product(s)?", n=len(self._ids))):
            return
        ok, result = self._run(lambda: be.apply(self.conn, self.user_id, self._ids, self._changes), title=tr("Apply"))
        if not ok:
            self.do_preview()               # prices may have changed since the preview
            return
        self._stale()
        self.tbl_preview.setRowCount(0)
        message = tr("Changed {n} product(s).", n=result["products"])
        self.lbl_summary.setText(message)
        self._info(tr("Apply"), message)
        self.load()


def build(conn, session, folder):
    return BulkEditScreen(conn, session)
