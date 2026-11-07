"""Products menu: search the products, add a new one with its size/colour grid, open one for editing.

No rules and no SQL here. core.product_list reads, core.matrix_entry adds, ui.products_detail edits.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLineEdit, QMessageBox,
    QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.matrix_entry import save_matrix_product
from core.money import format_rupees
from core.permissions import PermissionDenied, has_permission
from core.product_edit import DuplicateProductError
from core.product_list import list_products
from ui.i18n import tr
from ui.size_set_widgets import SizeSetPicker
from ui.widgets import form, label

MENU_TEXT = "Products"
log = logging.getLogger(__name__)
_COLUMNS = ("Name", "Category", "Brand", "Sizes/colours", "In stock", "Price", "Status")


def _show_error(parent, exc):
    if isinstance(exc, (ValueError, PermissionDenied)):
        QMessageBox.warning(parent, tr("Products"), tr(str(exc)))
    else:
        log.exception("Products screen action failed")
        QMessageBox.warning(parent, tr("Products"), tr("Something went wrong. Nothing was changed."))


class NewProductDialog(QDialog):
    def __init__(self, parent=None, conn=None, user_id=None):
        super().__init__(parent)
        self.setWindowTitle(tr("New product"))
        self.edits = {k: QLineEdit() for k in ("name", "category", "price", "cost", "sizes", "colours")}
        self.edits["sizes"].setPlaceholderText(tr("S, M, L, XL"))
        self.edits["colours"].setPlaceholderText(tr("Blue, Black"))
        rows = form()
        for key, text in (("name", "Name"), ("category", "Category"), ("price", "Selling price"),
                          ("cost", "Cost"), ("sizes", "Sizes (comma separated)"),
                          ("colours", "Colours (comma separated)")):
            if key == "sizes" and conn is not None:
                self.size_picker = SizeSetPicker(conn, user_id, self.edits["sizes"])
                rows.addRow(tr("Size set"), self.size_picker)
            rows.addRow(tr(text), self.edits[key])
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        col = QVBoxLayout(self)
        col.addLayout(rows)
        col.addWidget(buttons)

    def values(self) -> dict:
        t = {k: e.text() for k, e in self.edits.items()}
        return dict(name=t["name"], category=t["category"], price_text=t["price"], cost_text=t["cost"],
                    sizes_text=t["sizes"], colours_text=t["colours"])


class ProductsScreen(QWidget):
    def __init__(self, conn, session):
        super().__init__()
        self.conn, self.session = conn, session
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._detail = None
        self._ids = []

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search name, brand, category, SKU, barcode, size, colour"))
        self.search.textChanged.connect(self.refresh)
        self.inactive = QCheckBox(tr("Show hidden products"))
        self.inactive.toggled.connect(self.refresh)
        self.new_btn = QPushButton(tr("New product"))
        self.new_btn.setObjectName("primary")
        self.new_btn.setEnabled(has_permission(conn, session.user_id, "edit_products"))
        self.new_btn.clicked.connect(self._new)
        self.open_btn = QPushButton(tr("Open"))
        self.open_btn.clicked.connect(self._open_selected)
        self.message = label("", "subtitle")

        bar = QHBoxLayout()
        bar.addWidget(self.search, 1)
        bar.addWidget(self.inactive)
        bar.addWidget(self.open_btn)
        bar.addWidget(self.new_btn)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in _COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.doubleClicked.connect(lambda *_: self._open_selected())

        listing = QWidget()
        listing.setObjectName("page")
        col = QVBoxLayout(listing)
        col.setContentsMargins(18, 14, 18, 14)
        col.addWidget(label(tr("Products"), "title", wrap=False))
        col.addLayout(bar)
        col.addWidget(self.message)
        col.addWidget(self.table, 1)

        self.stack = QStackedWidget()
        self.stack.addWidget(listing)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stack)
        self.refresh()

    def refresh(self, *_):
        if self.stack.currentIndex() != 0:
            return
        try:
            rows = list_products(self.conn, self.session.user_id, self.search.text(),
                                 include_inactive=self.inactive.isChecked())
        except Exception as exc:  # noqa: BLE001
            self.message.setText(tr(str(exc)) if isinstance(exc, (ValueError, PermissionDenied))
                                 else tr("The list could not be loaded."))
            if not isinstance(exc, (ValueError, PermissionDenied)):
                log.exception("Product list failed")
            return
        self.message.setText("" if rows else tr("No products found."))
        self._ids = [r["id"] for r in rows]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = (r["name"], r["category"] or "", r["brand"] or "", str(r["variant_count"]),
                     str(r["sellable_qty"]), format_rupees(r["price_paise"] or 0),
                     tr("Active") if r["is_active"] else tr("Hidden"))
            for j, text in enumerate(cells):
                self.table.setItem(i, j, QTableWidgetItem(text))

    def _new(self):
        dialog = NewProductDialog(self, self.conn, self.session.user_id)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        try:
            try:
                result = save_matrix_product(self.conn, self.session.user_id, **values)
            except DuplicateProductError as dup:
                ask = QMessageBox.question(self, tr("Products"), tr(str(dup)))
                if ask != QMessageBox.Yes:
                    return
                result = save_matrix_product(self.conn, self.session.user_id, allow_duplicate=True, **values)
        except Exception as exc:  # noqa: BLE001
            _show_error(self, exc)
            return
        self.refresh()
        self.message.setText(tr("Product saved with {n} size/colour combination(s).", n=len(result["variant_ids"])))

    def _open_selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._ids):
            self.message.setText(tr("Select a product first."))
            return
        from ui.products_detail import ProductDetailView
        self._detail = ProductDetailView(self.conn, self.session, self._ids[row])
        back = QPushButton(tr("← Back to products"))
        back.clicked.connect(self._close_detail)
        holder = QWidget()
        holder.setObjectName("page")
        col = QVBoxLayout(holder)
        col.setContentsMargins(18, 10, 18, 0)
        col.addWidget(back, 0, Qt.AlignLeft)
        col.addWidget(self._detail, 1)
        self._holder = holder
        self.stack.addWidget(holder)
        self.stack.setCurrentWidget(holder)

    def _close_detail(self):
        self.stack.setCurrentIndex(0)
        self.stack.removeWidget(self._holder)
        self._holder.deleteLater()
        self._holder = self._detail = None
        self.refresh()


def build(conn, session, folder):
    return ProductsScreen(conn, session)
