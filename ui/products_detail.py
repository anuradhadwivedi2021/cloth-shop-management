"""Product detail: edit one product, see every size/colour with its stock, add more, hide or show, set racks.

The screen holds no rules and runs no SQL. It calls core.product_detail (read, add sizes/colours, save),
core.product_edit (hide/show, rack) and core.matrix_entry (what typed sizes/colours mean); core checks the
permissions, writes the audit log and decides what is valid. The screen only says "saved" after core
returned without an error, and it states exactly what was saved. Errors shown are short, no technical text.

    view = ProductDetailView(conn, session, product_id)
    view.changed.connect(...)        # emitted after anything was really changed (refresh the product list)
"""

import logging
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.money import format_rupees
from core.permissions import PermissionDenied
from core.product_detail import (
    DETAIL_FIELDS, add_variants, get_product_detail, preview_new_variants, rupees_text, save_changes,
)
from core.product_edit import set_product_active, set_rack_location, set_variant_active
from ui.i18n import N_, tr
from ui.widgets import form, label

log = logging.getLogger(__name__)

DOUBLE_CLICK_SECONDS = 0.6  # a second click this soon after a successful change is ignored (double-click)

_FIELD_LABELS = {
    "name": N_("Name"), "category": N_("Category"), "brand": N_("Brand"), "hsn": N_("HSN code"),
    "material": N_("Material"), "season": N_("Season"), "description": N_("Description"),
}
_UNIT_NAMES = (N_("piece"), N_("pair"), N_("set"), N_("metre"))  # shown with tr(unit)
_COLUMNS = (N_("Size"), N_("Colour"), N_("SKU"), N_("Barcode"), N_("Rack"), N_("In stock"), N_("Price"), N_("Status"))


class ProductDetailView(QWidget):
    changed = Signal()  # something was really saved: the product list should refresh

    def __init__(self, conn, session, product_id, parent=None):
        super().__init__(parent)
        self.conn, self.session = conn, session
        self.product_id = product_id
        self.detail = None
        self._variants = []
        self._busy = False
        self._clock = time.monotonic
        self._last_change = -1e9
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build()
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save)
        self.refresh()

    # ------------------------------------------------------------ layout

    def _build(self):
        self.title = label("", "title", wrap=False)
        self.state_label = label("", "subtitle", wrap=False)
        self.stock_label = label("", "subtitle", wrap=False)
        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(lambda: self._run(self._toggle_product))
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addWidget(self.state_label)
        head.addStretch(1)
        head.addWidget(self.stock_label)
        head.addWidget(self.toggle_btn)

        self.message = label("", None)  # the one place that says what happened
        self.message.setAccessibleName("status")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(8)
        outer.addLayout(head)
        outer.addWidget(self.message)

        body = QWidget()
        body.setObjectName("page")
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(12)
        column.addWidget(self._details_card())
        column.addWidget(self._variants_card(), 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def _card(self, title):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(label(tr(title), "subtitle", wrap=False))
        return frame, layout

    def _details_card(self):
        frame, layout = self._card(N_("Details"))
        self.edits = {key: QLineEdit() for key in DETAIL_FIELDS}
        self.gst_edit, self.reorder_edit = QLineEdit(), QLineEdit()
        self.price_edit, self.cost_edit, self.mrp_edit = QLineEdit(), QLineEdit(), QLineEdit()
        self.mrp_edit.setPlaceholderText(tr("Leave empty if none"))
        self.unit_combo = QComboBox()

        left, right, money = form(), form(), form()
        for key in ("name", "category", "brand", "hsn"):
            left.addRow(tr(_FIELD_LABELS[key]), self.edits[key])
        left.addRow(tr("GST rate (%)"), self.gst_edit)
        for key in ("material", "season", "description"):
            right.addRow(tr(_FIELD_LABELS[key]), self.edits[key])
        right.addRow(tr("Unit"), self.unit_combo)
        right.addRow(tr("Reorder level"), self.reorder_edit)
        money.addRow(tr("Selling price"), self.price_edit)
        money.addRow(tr("Cost"), self.cost_edit)
        money.addRow(tr("MRP"), self.mrp_edit)
        self.cost_row_label = money.labelForField(self.cost_edit)

        columns = QHBoxLayout()
        columns.setSpacing(28)
        for part in (left, right, money):
            columns.addLayout(part, 1)
        layout.addLayout(columns)

        self.save_btn = QPushButton(tr("Save changes  (Ctrl+S)"))
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self._save)
        self.read_only_hint = label(tr("You can look at this product but not change it."), "hint")
        row = QHBoxLayout()
        row.addWidget(self.read_only_hint, 1)
        row.addWidget(self.save_btn)
        layout.addLayout(row)
        return frame

    def _variants_card(self):
        frame, layout = self._card(N_("Sizes and colours"))
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in _COLUMNS])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setMinimumHeight(220)
        layout.addWidget(self.table)
        layout.addWidget(label(tr("Click a row to select it. Hold Ctrl or Shift to select many."), "hint"))

        self.rack_edit = QLineEdit()
        self.rack_edit.setPlaceholderText(tr("Rack or shelf, for example A-3"))
        self.rack_btn = QPushButton(tr("Set rack for selected"))
        self.rack_btn.clicked.connect(lambda: self._run(self._set_rack))
        self.hide_btn = QPushButton(tr("Hide selected"))
        self.hide_btn.clicked.connect(lambda: self._run(lambda: self._set_selected_active(False)))
        self.show_btn = QPushButton(tr("Show selected"))
        self.show_btn.clicked.connect(lambda: self._run(lambda: self._set_selected_active(True)))
        tools = QHBoxLayout()
        tools.addWidget(self.rack_edit, 1)
        tools.addWidget(self.rack_btn)
        tools.addWidget(self.hide_btn)
        tools.addWidget(self.show_btn)
        layout.addLayout(tools)

        layout.addWidget(label(tr("Add more sizes and colours (separate with commas)"), "subtitle", wrap=False))
        self.new_sizes, self.new_colours = QLineEdit(), QLineEdit()
        self.new_sizes.setPlaceholderText(tr("Sizes, for example XL, XXL"))
        self.new_colours.setPlaceholderText(tr("Colours, for example Blue, Red"))
        self.new_sizes.textChanged.connect(self._update_preview)
        self.new_colours.textChanged.connect(self._update_preview)
        self.add_btn = QPushButton(tr("Add"))
        self.add_btn.clicked.connect(lambda: self._run(self._add_variants))
        add_row = QHBoxLayout()
        add_row.addWidget(self.new_sizes, 1)
        add_row.addWidget(self.new_colours, 1)
        add_row.addWidget(self.add_btn)
        layout.addLayout(add_row)
        self.preview_label = label("", "hint")
        layout.addWidget(self.preview_label)
        return frame

    # ------------------------------------------------------------ showing

    def refresh(self):
        """Reload from the database. Also called each time the page is opened."""
        try:
            self.detail = get_product_detail(self.conn, self.session.user_id, self.product_id)
        except Exception as exc:
            self.detail = None
            self._fail(exc, tr("This product could not be opened."))
            self._set_enabled(False, False, False)
            return
        self._fill(self.detail)

    def set_product(self, product_id):
        self.product_id = product_id
        self.new_sizes.clear()
        self.new_colours.clear()
        self.refresh()

    def _fill(self, detail):
        product, can, totals = detail["product"], detail["can"], detail["totals"]
        self.title.setText(product["name"])
        self.state_label.setText(tr("Active") if product["is_active"] else tr("Hidden from selling"))
        self.stock_label.setText(tr("In stock: {n}", n=totals["sellable_qty"]))
        self.toggle_btn.setText(tr("Hide product") if product["is_active"] else tr("Show product"))

        for key, edit in self.edits.items():
            edit.setText(product[key] or "")
        gst = product["gst_rate_percent"]
        self.gst_edit.setText("" if gst is None else f"{gst:g}")
        self.reorder_edit.setText(str(product["reorder_level"]))
        self.price_edit.setText(rupees_text(product["price_paise"]))
        self.mrp_edit.setText(rupees_text(product["mrp_paise"]))
        self.cost_edit.setText(rupees_text(product["cost_paise"]))
        self.unit_combo.clear()
        for unit in detail["units"]:
            self.unit_combo.addItem(tr(unit), unit)
        self.unit_combo.setCurrentIndex(max(self.unit_combo.findData(product["unit"]), 0))

        self._variants = detail["variants"]
        self.table.setRowCount(len(self._variants))
        for row, v in enumerate(self._variants):
            values = (
                v["size"], v["colour"], v["sku"], v["barcode"] or "", v["rack_location"] or "",
                str(v["sellable_qty"]), format_rupees(v["price_paise"]),
                tr("Active") if v["is_active"] else tr("Hidden"),
            )
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 5:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if not v["is_active"] or (col == 5 and v["sellable_qty"] <= 0):
                    item.setForeground(Qt.gray)
                self.table.setItem(row, col, item)

        self._set_enabled(can["edit"], can["change_price"], can["view_cost"])
        self._update_preview()

    def _set_enabled(self, edit, price, cost):
        for widget in (*self.edits.values(), self.gst_edit, self.reorder_edit, self.unit_combo,
                       self.new_sizes, self.new_colours, self.rack_edit):
            widget.setEnabled(edit)
        for widget in (self.price_edit, self.mrp_edit, self.cost_edit):
            widget.setEnabled(edit and price)
        for widget in (self.save_btn, self.toggle_btn, self.add_btn, self.rack_btn, self.hide_btn, self.show_btn):
            widget.setEnabled(edit)
        self.cost_edit.setVisible(cost)
        if self.cost_row_label is not None:
            self.cost_row_label.setVisible(cost)
        self.read_only_hint.setVisible(not edit)

    def _update_preview(self, *_):
        self.preview_label.setText("")
        if not (self.new_sizes.text().strip() and self.new_colours.text().strip()) or self.detail is None:
            return
        try:
            found = preview_new_variants(
                self.conn, self.session.user_id, self.product_id, self.new_sizes.text(), self.new_colours.text()
            )
        except (ValueError, PermissionDenied):
            return
        text = tr("{n} new to add", n=len(found["new"]))
        if found["existing"]:
            text += "  ·  " + tr("{n} already exist and will be skipped", n=len(found["existing"]))
        self.preview_label.setText(text)

    # ------------------------------------------------------------ messages

    def _ok(self, text):
        self.message.setObjectName("ok")
        self._restyle(text)

    def _error(self, text):
        self.message.setObjectName("error")
        self._restyle(text)

    def _info(self, text):
        self.message.setObjectName("subtitle")
        self._restyle(text)

    def _restyle(self, text):
        self.message.setText(text)
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)

    def _fail(self, exc, fallback=None):
        """Show a short, safe reason. Rules from core are written for users; anything else is logged only."""
        if isinstance(exc, (ValueError, PermissionDenied)):
            self._error(tr(str(exc)))
        else:
            log.exception("Product detail action failed")
            self._error(fallback or tr("Something went wrong. Nothing was changed."))

    def _confirm(self, text) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def _run(self, action):
        """Run one action at a time. A double-click cannot do it twice: the second click arrives while the
        first is running, or right after it finished (then it would only toggle or complain about the result)."""
        if self._busy or self._clock() - self._last_change < DOUBLE_CLICK_SECONDS:
            return
        self._busy = True
        try:
            action()
        except Exception as exc:  # core raised: nothing was said as saved
            self._fail(exc)
        finally:
            self._busy = False

    def _changed(self, text):
        self.refresh()
        self._ok(text)  # after the refresh, so the confirmation stays visible
        self._last_change = self._clock()
        self.changed.emit()

    # ------------------------------------------------------------ actions

    def _save(self):
        self._run(self._save_now)

    def _save_now(self):
        detail = self.detail
        if detail is None or not detail["can"]["edit"]:
            return
        prices = None
        if detail["can"]["change_price"]:
            prices = {"price": self.price_edit.text(), "mrp": self.mrp_edit.text()}
            if detail["can"]["view_cost"]:
                prices["cost"] = self.cost_edit.text()
        result = save_changes(
            self.conn, self.session.user_id, self.product_id,
            fields={key: edit.text() for key, edit in self.edits.items()},
            prices=prices, gst_text=self.gst_edit.text(), reorder_text=self.reorder_edit.text(),
            unit=self.unit_combo.currentData(),
        )
        count = len(result["details"]) + len(result["prices"])
        if result["error"]:
            self.refresh()
            self._error(tr("The details were saved, but the prices were not: {reason}", reason=tr(result["error"])))
            self._last_change = self._clock()
            self.changed.emit()
        elif count:
            self._changed(tr("Saved {n} change(s).", n=count))
        else:
            self._info(tr("There is nothing new to save."))

    def _toggle_product(self):
        if self.detail is None:
            return
        make_active = not self.detail["product"]["is_active"]
        left = self.detail["totals"]["sellable_qty"]
        if not make_active and left > 0 and not self._confirm(
            tr("{n} pieces are still in stock. Hide this product anyway?", n=left)
        ):
            return
        outcome = set_product_active(self.conn, self.session.user_id, self.product_id, make_active)
        if not outcome["changed"]:
            self.refresh()
            self._info(tr("Nothing changed."))
            return
        self._changed(tr("The product is active again.") if make_active else tr("The product is hidden from selling."))

    def _selected(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self._variants[r] for r in rows if 0 <= r < len(self._variants)]

    def _need_selection(self):
        chosen = self._selected()
        if not chosen:
            self._error(tr("First select one or more sizes in the table."))
        return chosen

    def _set_rack(self):
        chosen = self._need_selection()
        if not chosen:
            return
        rack = self.rack_edit.text().strip()
        changed = set_rack_location(self.conn, self.session.user_id, [v["id"] for v in chosen], rack)
        self.rack_edit.clear()
        if not changed:
            self._info(tr("Nothing changed."))
        elif rack:
            self._changed(tr("Rack {rack} set for {n} item(s).", rack=rack, n=changed))
        else:
            self._changed(tr("Rack cleared for {n} item(s).", n=changed))

    def _set_selected_active(self, make_active):
        chosen = [v for v in self._need_selection() if v["is_active"] != make_active]
        if not chosen:
            if self._selected():
                self._info(tr("Nothing changed."))
            return
        left = sum(v["sellable_qty"] for v in chosen)
        if not make_active and left > 0 and not self._confirm(
            tr("{n} pieces are still in stock for these sizes. Hide them anyway?", n=left)
        ):
            return
        done = 0
        try:
            for v in chosen:
                set_variant_active(self.conn, self.session.user_id, v["id"], make_active)
                done += 1
        except Exception as exc:
            self.refresh()
            if done:
                self.changed.emit()
            self._fail(exc)
            if done:
                self._error(tr("Only {done} of {total} were changed. {reason}", done=done, total=len(chosen),
                               reason=self.message.text()))
            return
        self._changed(tr("{n} size(s) shown again.", n=done) if make_active else tr("{n} size(s) hidden.", n=done))

    def _add_variants(self):
        result = add_variants(
            self.conn, self.session.user_id, self.product_id, self.new_sizes.text(), self.new_colours.text()
        )
        self.new_sizes.clear()
        self.new_colours.clear()
        text = tr("{n} new size/colour combination(s) added.", n=len(result["variant_ids"]))
        if result["skipped"]:
            text += " " + tr("{n} already existed.", n=result["skipped"])
        self._changed(text)