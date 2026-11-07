"""Stock and Variants menu: stock of every size/colour, with filters. Read only (no SQL, no rules here).

Quantities come from core.reports_stock.stock_overview (stock movements). Damaged, pending-check and
other buckets are shown in their own columns and are never added to the sellable quantity.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLineEdit, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.permissions import PermissionDenied
from core.reports_stock import stock_overview
from ui.i18n import tr
from ui.widgets import label

MENU_TEXT = "Stock and Variants"
log = logging.getLogger(__name__)
_COLUMNS = ("Product", "Size", "Colour", "SKU", "Barcode", "Rack", "Sellable", "Damaged", "Pending check",
            "Reorder level", "Status")
_STATUS = {"ok": "OK", "low": "Low", "out": "Out of stock"}


class StockScreen(QWidget):
    def __init__(self, conn, session):
        super().__init__()
        self.conn, self.session = conn, session
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
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()

        col = QVBoxLayout(self)
        col.setContentsMargins(18, 14, 18, 14)
        col.addWidget(label(tr("Stock and Variants"), "title", wrap=False))
        col.addLayout(bar)
        col.addWidget(self.message)
        col.addWidget(self.table, 1)
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
        total = sum(r["sellable_qty"] for r in rows)
        self.message.setText(tr("{n} variant(s), {q} piece(s) available to sell.", n=len(rows), q=total)
                             if rows else tr("No stock found."))
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = (r["product"], r["size"], r["colour"], r["sku"] or "", r["barcode"] or "",
                     r["rack_location"] or "", r["sellable_qty"], r["damaged_qty"], r["returned_pending_qty"],
                     r["reorder_level"] or 0, tr(_STATUS[r["status"]]))
            for j, value in enumerate(cells):
                item = QTableWidgetItem(str(value))
                if isinstance(value, int):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, j, item)


def build(conn, session, folder):
    return StockScreen(conn, session)
