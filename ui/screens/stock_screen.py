"""Stock and Variants menu: stock of every size/colour, with filters. No SQL and no rules here.

Barcode labels (SRS STK-05): select one or more rows, choose how many labels of each, and print them
(or save them as a PDF to check first). The label size comes from Settings. Needs "print_labels".

Quantities come from core.reports_stock.stock_overview (stock movements). Damaged, pending-check and
other buckets are shown in their own columns and are never added to the sellable quantity.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.labels import MAX_COPIES_PER_VARIANT
from core.permissions import PermissionDenied, has_permission
from core.reports_stock import stock_overview
from ui import print_service
from ui.i18n import tr
from ui.widgets import label

MENU_TEXT = "Stock and Variants"
log = logging.getLogger(__name__)
_COLUMNS = ("Product", "Size", "Colour", "SKU", "Barcode", "Rack", "Sellable", "Damaged", "Pending check",
            "On trial", "Reserved", "Reorder level", "Status")
_STATUS = {"ok": "OK", "low": "Low", "out": "Out of stock"}


class StockScreen(QWidget):
    def __init__(self, conn, session):
        super().__init__()
        self.conn, self.session = conn, session
        self._rows = []   # the rows shown, in the same order as the table
        self.can_labels = has_permission(conn, session.user_id, "print_labels")
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search product, size, colour, SKU, barcode, rack"))
        self.search.textChanged.connect(self.refresh)
        self.status = QComboBox()
        for value, text in ((None, "All stock"), ("low", "Low stock"), ("out", "Out of stock"), ("ok", "In stock")):
            self.status.addItem(tr(text), value)
        self.status.currentIndexChanged.connect(self.refresh)
        self.hidden = QCheckBox(tr("Show hidden items"))
        self.hidden.toggled.connect(self.refresh)
        self.message = label("", "subtitle")

        bar = QHBoxLayout()
        bar.addWidget(self.search, 1)
        bar.addWidget(self.status)
        bar.addWidget(self.hidden)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in _COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()

        self.copies = QSpinBox()
        self.copies.setRange(1, MAX_COPIES_PER_VARIANT)
        self.btn_labels = QPushButton(tr("Print barcode labels"))
        self.btn_labels.setObjectName("primary")
        self.btn_labels_pdf = QPushButton(tr("Save labels as PDF"))
        self.btn_labels.clicked.connect(self.print_labels)
        self.btn_labels_pdf.clicked.connect(self.save_labels_pdf)
        label_bar = QHBoxLayout()
        label_bar.addWidget(label(tr("Labels of each selected item:"), "hint", wrap=False))
        label_bar.addWidget(self.copies)
        label_bar.addWidget(self.btn_labels)
        label_bar.addWidget(self.btn_labels_pdf)
        label_bar.addStretch(1)
        if not self.can_labels:
            for widget in (self.copies, self.btn_labels, self.btn_labels_pdf):
                widget.setEnabled(False)
            self.btn_labels.setToolTip(tr("You are not allowed to print barcode labels."))

        col = QVBoxLayout(self)
        col.setContentsMargins(18, 14, 18, 14)
        col.addWidget(label(tr("Stock and Variants"), "title", wrap=False))
        col.addLayout(bar)
        col.addWidget(self.message)
        col.addWidget(self.table, 1)
        col.addLayout(label_bar)
        self.refresh()

    def refresh(self, *_):
        try:
            rows = stock_overview(self.conn, user_id=self.session.user_id, search=self.search.text(),
                                  status=self.status.currentData(), include_inactive=self.hidden.isChecked())
        except Exception as exc:  # noqa: BLE001
            known = isinstance(exc, (ValueError, PermissionDenied))
            if not known:
                log.exception("Stock list failed")
            self.message.setText(tr(str(exc)) if known else tr("The stock list could not be loaded."))
            return
        self._rows = rows
        total = sum(r["sellable_qty"] for r in rows)
        self.message.setText(tr("{n} variant(s), {q} piece(s) available to sell.", n=len(rows), q=total)
                             if rows else tr("No stock found."))
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = (r["product"], r["size"], r["colour"], r["sku"] or "", r["barcode"] or "",
                     r["rack_location"] or "", r["sellable_qty"], r["damaged_qty"], r["returned_pending_qty"],
                     r["on_trial_qty"], r["reserved_qty"], r["reorder_level"] or 0, tr(_STATUS[r["status"]]))
            for j, value in enumerate(cells):
                item = QTableWidgetItem(str(value))
                if isinstance(value, int):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, j, item)

    # ------------------------------------------------------------ barcode labels

    def _label_items(self):
        """[{variant_id, copies}] for the selected rows, or None (with a message) when nothing is selected."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            self.message.setText(tr("Select the items you want labels for."))
            return None
        return [{"variant_id": self._rows[r]["variant_id"], "copies": self.copies.value()} for r in rows
                if 0 <= r < len(self._rows)]

    def _show(self, result):
        self.message.setText(result.message)

    def print_labels(self):
        items = self._label_items()
        if items:
            self._show(print_service.print_labels(self.conn, self.session, items, parent=self))

    def save_labels_pdf(self):
        items = self._label_items()
        if not items:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save labels as PDF"), "labels.pdf", "PDF (*.pdf)")
        if path:
            self._show(print_service.save_labels_pdf(self.conn, self.session, items, path))


def build(conn, session, folder):
    return StockScreen(conn, session)
