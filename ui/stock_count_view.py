# ui/stock_count_view.py
"""Stock Count and Adjustments screen (SRS 7.4 and 7.5, STK-04).

The screen only shows things and passes what was typed to the tested core code
(core.stock_counts and core.stock_ops). The permission checks, the "stock changed after the count
started" check, the reasons and the audit entries all live there, so they hold even if this screen is wrong.

Two tabs:
  * Stock count: start a count (everything, one category or one rack, optionally blind), enter what was
    counted, review the differences, and let the owner approve. Approving turns each difference into ONE
    stock movement and keeps the before-and-after quantities. Nothing is ever overwritten silently.
  * Damaged or missing: take pieces out of sellable stock with a reason. They are moved to the damaged
    bucket (or recorded as missing), never deleted.

Rules kept here:
  * "Saved" or "applied" is shown only after the core call really returned.
  * A blind count never shows the system quantity while it is open.
  * Only a user with 'approve_stock_count' (the owner by default) can approve; a storekeeper can count.
  * Technical detail of a fault goes to the log only. No lambdas are connected to signals, because a
    lambda that holds the screen can crash Qt when the screen closes.
"""

import logging

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QInputDialog, QLineEdit, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from core import bill_preview as bp
from core import stock_counts as sc
from core import stock_ops
from core.permissions import has_permission
from core.purchase_service import format_ui_date
from ui.i18n import N_, tr
from ui.sales_returns_view import ReplacementDialog
from ui.stock_holds_panel import StockHoldsPanel
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import form, label

log = logging.getLogger("clothshop")

STATUS_TEXT = {"open": N_("Open"), "applied": N_("Applied"), "cancelled": N_("Cancelled")}
KIND_TEXT = {
    "damaged": N_("Damaged"),
    "missing": N_("Missing"),
    "returned_ok": N_("Returned, fit for resale"),
    "returned_damaged": N_("Returned, damaged"),
}
NOT_COUNTED = -1  # the value a count box shows as "Not counted"


class ItemDialog(ReplacementDialog):
    """Search an item and pick its size and colour (the same dialog the exchange screen uses)."""

    def __init__(self, conn, parent=None):
        super().__init__(conn, parent)
        self.setWindowTitle(tr("Choose the item"))


def _signed(number: int) -> str:
    return f"{number:+d}" if number else "0"


class StockCountView(ScreenBase, QWidget):
    """Count the stock on the shelves, approve the differences, and record damaged or missing pieces."""

    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.can_approve = has_permission(conn, self.user_id, "approve_stock_count")
        self._counts = []        # rows of the counts list
        self._count_id = None    # the count that is open on the sheet
        self._count = None       # its header row
        self._lines = []         # the sheet lines
        self._spins = []         # one counted-quantity box per line
        self._reasons = []       # one reason box per line
        self._diff_lines = []    # lines shown in the differences table
        self._diff_reasons = []
        self._item = None        # the item chosen on the damaged / missing tab
        self._busy = False
        self._saved = 0          # lines written by the last save_counts()

        self.lbl_status = label("", "error")
        self.tabs = QTabWidget()

        # ================= tab 1: stock count =================
        page = QWidget()
        col = QVBoxLayout(page)

        start = QGroupBox(tr("Start a new count"))
        start_form = form()
        self.cmb_category = QComboBox()
        self.txt_rack = QLineEdit()
        self.txt_rack.setPlaceholderText(tr("Rack or shelf, for example A1 (optional)"))
        self.chk_blind = QCheckBox(tr("Blind count: hide the system quantity while counting"))
        self.txt_note = QLineEdit()
        self.txt_note.setPlaceholderText(tr("Note (optional)"))
        start_form.addRow(tr("What to count"), self.cmb_category)
        start_form.addRow(tr("Rack"), self.txt_rack)
        start_form.addRow("", self.chk_blind)
        start_form.addRow(tr("Note"), self.txt_note)
        start_col = QVBoxLayout(start)
        start_col.addLayout(start_form)
        self.btn_start = QPushButton(tr("Start count"))
        start_col.addWidget(self.btn_start)
        col.addWidget(start)

        self.tbl_counts = make_table([
            tr("Count"), tr("Started"), tr("What is counted"), tr("Status"), tr("Counted so far"), tr("Blind"),
        ])
        col.addWidget(self.tbl_counts, 1)

        self.lbl_hint = label("", "hint")
        col.addWidget(self.lbl_hint)
        self.tbl_sheet = make_table([
            tr("Product"), tr("Size"), tr("Colour"), tr("Rack"), tr("System quantity"), tr("Counted"),
            tr("Difference"), tr("Reason for the difference"),
        ])
        col.addWidget(self.tbl_sheet, 3)

        buttons = QHBoxLayout()
        self.btn_save = QPushButton(tr("Save counted quantities"))
        self.btn_review = QPushButton(tr("Show differences"))
        self.btn_apply = QPushButton(tr("Approve and update stock"))
        self.btn_apply.setObjectName("primary")
        self.btn_cancel = QPushButton(tr("Cancel this count"))
        for b in (self.btn_save, self.btn_review, self.btn_apply, self.btn_cancel):
            buttons.addWidget(b)
        col.addLayout(buttons)

        self.box_diff = QGroupBox(tr("Differences to approve"))
        diff_col = QVBoxLayout(self.box_diff)
        self.tbl_diff = make_table([
            tr("Product"), tr("Size"), tr("Colour"), tr("Before"), tr("Counted"), tr("Difference"),
            tr("Reason for the difference"),
        ])
        diff_col.addWidget(self.tbl_diff)
        col.addWidget(self.box_diff, 2)
        self.tabs.addTab(page, tr("Stock count"))

        # ================= tab 2: damaged or missing =================
        other = QWidget()
        oc = QVBoxLayout(other)
        oc.addWidget(label(tr("Damaged or missing pieces leave sellable stock. Nothing is deleted."), "hint"))
        pick = QHBoxLayout()
        self.btn_choose_item = QPushButton(tr("Choose item"))
        self.lbl_item = label(tr("No item chosen yet."))
        pick.addWidget(self.btn_choose_item)
        pick.addWidget(self.lbl_item, 1)
        oc.addLayout(pick)
        adj = QHBoxLayout()
        self.spin_qty = QSpinBox()
        self.spin_qty.setRange(1, 1)
        self.txt_adj_reason = QLineEdit()
        self.txt_adj_reason.setPlaceholderText(tr("What happened? (required)"))
        self.btn_damaged = QPushButton(tr("Mark as damaged"))
        self.btn_missing = QPushButton(tr("Mark as missing"))
        adj.addWidget(label(tr("Pieces")))
        adj.addWidget(self.spin_qty)
        adj.addWidget(self.txt_adj_reason, 1)
        adj.addWidget(self.btn_damaged)
        adj.addWidget(self.btn_missing)
        oc.addLayout(adj)
        self.tbl_adjustments = make_table([
            tr("Date"), tr("What happened"), tr("Product"), tr("Size"), tr("Colour"), tr("Pieces"), tr("Reason"),
            tr("By"),
        ])
        oc.addWidget(self.tbl_adjustments, 1)
        self.tabs.addTab(other, tr("Damaged or missing"))
        self.holds_panel = StockHoldsPanel(conn, session)
        self.tabs.addTab(self.holds_panel, tr("Trial and reserved"))

        outer = QVBoxLayout(self)
        outer.addWidget(label(tr("Stock Count and Adjustments"), "title"))
        outer.addWidget(self.lbl_status)
        outer.addWidget(self.tabs, 1)

        self.btn_start.clicked.connect(self.start_count)
        self.tbl_counts.currentCellChanged.connect(self._count_selected)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_review.clicked.connect(self._on_review)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_choose_item.clicked.connect(self.choose_item)
        self.btn_damaged.clicked.connect(self._on_damaged)
        self.btn_missing.clicked.connect(self._on_missing)
        self._update_buttons()
        self.refresh()

    # ------------------------------------------------------------ small pop-ups (tests replace these)

    def _ask_text(self, title, text):
        value, ok = QInputDialog.getText(self, title, text)
        return value.strip() if ok else None

    # ------------------------------------------------------------ loading

    def refresh(self) -> None:
        """Called each time the page is opened."""
        title = tr("Stock Count and Adjustments")
        ok, categories = self._run(lambda: sc.count_categories(self.conn, user_id=self.user_id), title=title,
                                   quiet_to=self.lbl_status)
        keep = self.cmb_category.currentData()
        self.cmb_category.blockSignals(True)
        self.cmb_category.clear()
        self.cmb_category.addItem(tr("Everything in the shop"), None)
        for name in categories or []:
            self.cmb_category.addItem(name, name)
        self.cmb_category.setCurrentIndex(max(self.cmb_category.findData(keep), 0))
        self.cmb_category.blockSignals(False)
        self.reload_counts()
        self.reload_adjustments()

    def reload_counts(self, select_id=None) -> None:
        keep = select_id if select_id is not None else self._count_id
        ok, rows = self._run(lambda: sc.list_counts(self.conn, user_id=self.user_id),
                             title=tr("Stock Count and Adjustments"), quiet_to=self.lbl_status)
        self._counts = rows or []
        self.tbl_counts.blockSignals(True)
        self.tbl_counts.setRowCount(0)
        for c in self._counts:
            add_row(self.tbl_counts, [
                c["count_no"], format_ui_date(c["created_at"][:10]), c["scope"] or "",
                tr(STATUS_TEXT.get(c["status"], c["status"])), f"{c['counted_count']} / {c['line_count']}",
                tr("Yes") if c["blind"] else tr("No"),
            ], right={4})
        index = next((i for i, c in enumerate(self._counts) if c["id"] == keep), -1)
        self.tbl_counts.setCurrentCell(index, 0 if index >= 0 else -1)
        self.tbl_counts.blockSignals(False)
        self._count_selected()

    def _count_selected(self, *_) -> None:
        row = self.tbl_counts.currentRow()
        self._count_id = self._counts[row]["id"] if 0 <= row < len(self._counts) else None
        self._load_sheet()

    def _load_sheet(self) -> None:
        self.tbl_sheet.setRowCount(0)
        self.tbl_diff.setRowCount(0)
        self._lines, self._spins, self._reasons = [], [], []
        self._diff_lines, self._diff_reasons = [], []
        self._count = None
        self.lbl_hint.setText("")
        if self._count_id is None:
            self._update_buttons()
            return
        ok, sheet = self._run(lambda: sc.get_count_sheet(self.conn, self._count_id),
                              title=tr("Stock Count and Adjustments"), quiet_to=self.lbl_status)
        if not ok:
            self._update_buttons()
            return
        self._count, self._lines = sheet["count"], sheet["lines"]
        is_open = self._count["status"] == "open"
        hidden = bool(self._count["blind"]) and is_open
        if hidden:
            self.lbl_hint.setText(tr("Blind count: the system quantities stay hidden until the differences are reviewed."))
        elif not is_open:
            self.lbl_hint.setText(tr("This count is {status}. It can no longer be changed.",
                                     status=tr(STATUS_TEXT.get(self._count["status"], self._count["status"])).lower()))
        for line in self._lines:
            row = add_row(self.tbl_sheet, [
                line["product_name"], line["size"] or "", line["colour"] or "", line["rack_location"] or "",
                "—" if line["expected_qty"] is None else line["expected_qty"], "",
                "" if line["difference"] is None else _signed(line["difference"]), "",
            ], right={4, 6})
            spin = QSpinBox()
            spin.setRange(NOT_COUNTED, 999999)
            spin.setSpecialValueText(tr("Not counted"))
            spin.setValue(NOT_COUNTED if line["counted_qty"] is None else line["counted_qty"])
            spin.setEnabled(is_open)
            spin.valueChanged.connect(self._sheet_changed)
            self.tbl_sheet.setCellWidget(row, 5, spin)
            self._spins.append(spin)
            reason = QLineEdit(line["reason"] or "")
            reason.setEnabled(is_open)
            self.tbl_sheet.setCellWidget(row, 7, reason)
            self._reasons.append(reason)
        self._update_buttons()

    def _sheet_changed(self, *_) -> None:
        """Show the difference while counting (never for a blind count that is still open)."""
        if self._count is None or (self._count["blind"] and self._count["status"] == "open"):
            return
        for row, (line, spin) in enumerate(zip(self._lines, self._spins)):
            item = self.tbl_sheet.item(row, 6)
            if item is not None:
                item.setText("" if spin.value() == NOT_COUNTED else _signed(spin.value() - line["expected_qty"]))

    def _update_buttons(self) -> None:
        is_open = self._count is not None and self._count["status"] == "open" and not self._busy
        self.btn_save.setEnabled(is_open)
        self.btn_review.setEnabled(is_open)
        self.btn_cancel.setEnabled(is_open)
        self.btn_apply.setEnabled(is_open and self.can_approve)
        self.btn_apply.setToolTip("" if self.can_approve else tr("Only the owner can approve a stock count."))
        self.box_diff.setVisible(self._count is not None and self._count["status"] == "open")

    # ------------------------------------------------------------ the count

    def start_count(self) -> None:
        if self._busy:
            return
        title = tr("Stock Count and Adjustments")
        self._busy = True
        try:
            ok, count_id = self._run(
                lambda: sc.start_count(
                    self.conn, user_id=self.user_id, category=self.cmb_category.currentData(),
                    rack=self.txt_rack.text().strip() or None, blind=self.chk_blind.isChecked(),
                    note=self.txt_note.text().strip() or None),
                title=title)
        finally:
            self._busy = False
        if not ok:
            return
        self.txt_note.clear()
        self.reload_counts(select_id=count_id)
        self._info(title, tr("Count {no} started. Enter what is on the shelves, then save.", no=self._count["count_no"]
                             if self._count else ""))

    def save_counts(self) -> bool:
        """Save every counted quantity and reason that changed. Stops at the first problem."""
        if self._count is None:
            return False
        title = tr("Stock Count and Adjustments")
        saved = 0
        for line, spin, reason in zip(self._lines, self._spins, self._reasons):
            counted = spin.value()
            if counted == NOT_COUNTED:
                continue
            text = reason.text().strip() or None
            if counted == line["counted_qty"] and text == ((line["reason"] or "").strip() or None):
                continue
            ok, _ = self._run(
                lambda line=line, counted=counted, text=text: sc.record_count(
                    self.conn, self._count_id, line["variant_id"], counted, user_id=self.user_id, reason=text),
                title=title)
            if not ok:
                self._load_sheet()
                return False
            saved += 1
        self._saved = saved
        return True

    def _on_save(self, *_) -> None:
        if self._busy:
            return
        keep = self._count_id
        if self.save_counts():
            self.reload_counts(select_id=keep)
            self._info(tr("Stock Count and Adjustments"),
                       tr("{n} line(s) saved.", n=self._saved) if self._saved else tr("Nothing new to save."))

    def review_differences(self) -> bool:
        """Save what was counted, refresh the sheet, then list the lines that differ from the system quantity."""
        keep = self._count_id
        if not self.save_counts():
            return False
        self.reload_counts(select_id=keep)   # fresh sheet and progress; this also clears the differences table
        return self._fill_diff()

    def _fill_diff(self) -> bool:
        ok, rows = self._run(lambda: sc.variance_report(self.conn, self._count_id),
                             title=tr("Stock Count and Adjustments"))
        if not ok:
            return False
        self._diff_lines, self._diff_reasons = rows, []
        self.tbl_diff.setRowCount(0)
        for r in rows:
            row = add_row(self.tbl_diff, [
                r["product_name"], r["size"] or "", r["colour"] or "", r["expected_qty"], r["counted_qty"],
                _signed(r["difference"]), "",
            ], right={3, 4, 5})
            reason = QLineEdit(r["reason"] or "")
            self.tbl_diff.setCellWidget(row, 6, reason)
            self._diff_reasons.append(reason)
        self.lbl_status.setText("" if rows else tr("No differences: the counted quantities match the system."))
        return True

    def _on_review(self, *_) -> None:
        if not self._busy:
            self.review_differences()

    def _save_diff_reasons(self) -> bool:
        """The owner may write the reasons in the differences table; save them before approving."""
        for line, reason in zip(self._diff_lines, self._diff_reasons):
            text = reason.text().strip() or None
            if text == ((line["reason"] or "").strip() or None):
                continue
            ok, _ = self._run(
                lambda line=line, text=text: sc.record_count(
                    self.conn, self._count_id, line["variant_id"], line["counted_qty"], user_id=self.user_id,
                    reason=text),
                title=tr("Stock Count and Adjustments"))
            if not ok:
                return False
        return True

    def apply_count(self) -> None:
        """Owner approval: every difference becomes one stock movement."""
        if self._busy or self._count is None:
            return
        title = tr("Stock Count and Adjustments")
        if not self.can_approve:
            self._error(title, tr("Only the owner can approve a stock count."))
            return
        # The differences table is refilled from the saved data below; keep the reasons the owner typed in it.
        typed = {line["variant_id"]: box.text().strip()
                 for line, box in zip(self._diff_lines, self._diff_reasons) if box.text().strip()}
        if not self.review_differences():
            return
        for line, box in zip(self._diff_lines, self._diff_reasons):
            if line["variant_id"] in typed:
                box.setText(typed[line["variant_id"]])
        if not self._save_diff_reasons():
            return
        units = sum(abs(r["difference"]) for r in self._diff_lines)
        question = tr("Update the stock for {n} item(s), {units} piece(s) in total? "
                      "The old quantities are kept in the record. This cannot be undone.",
                      n=len(self._diff_lines), units=units)
        if not self._ask(title, question):
            return
        self._busy = True
        self._update_buttons()
        try:
            ok, summary = self._run(lambda: sc.apply_count(self.conn, self._count_id, approver_id=self.user_id),
                                    title=title)
        finally:
            self._busy = False
        if not ok:
            self._update_buttons()
            return
        self._info(title, tr("Stock count approved. {n} item(s) adjusted: {added} piece(s) added, {removed} removed.",
                             n=summary["variants_adjusted"], added=summary["units_added"],
                             removed=summary["units_removed"]))
        self.reload_counts()
        self.reload_adjustments()

    def _on_apply(self, *_) -> None:
        self.apply_count()

    def cancel_count(self) -> None:
        """Close the count without touching stock. A reason is required."""
        if self._busy or self._count is None:
            return
        title = tr("Stock Count and Adjustments")
        reason = self._ask_text(title, tr("Why is this count being cancelled?"))
        if reason is None:
            return
        self._busy = True
        try:
            ok, _ = self._run(lambda: sc.cancel_count(self.conn, self._count_id, user_id=self.user_id, reason=reason),
                              title=title)
        finally:
            self._busy = False
        if not ok:
            return
        self._info(title, tr("The count was cancelled. Stock was not changed."))
        self.reload_counts()

    def _on_cancel(self, *_) -> None:
        self.cancel_count()

    # ------------------------------------------------------------ damaged or missing

    def choose_item(self) -> None:
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
        self.btn_damaged.setEnabled(sellable > 0)
        self.btn_missing.setEnabled(sellable > 0)
        if sellable <= 0:
            self.lbl_status.setText(tr("{item} has nothing in sellable stock.", item=info["label"]))
        return True

    def reload_adjustments(self) -> None:
        ok, rows = self._run(lambda: stock_ops.list_recent_adjustments(self.conn, user_id=self.user_id),
                             title=tr("Stock Count and Adjustments"), quiet_to=self.lbl_status)
        self.tbl_adjustments.setRowCount(0)
        for r in rows or []:
            add_row(self.tbl_adjustments, [
                format_ui_date(r["created_at"][:10]), tr(KIND_TEXT.get(r["kind"], r["kind"])), r["product_name"],
                r["size"] or "", r["colour"] or "", r["qty"], r["reason"], r["username"] or "",
            ], right={5})

    def record_adjustment(self, kind: str) -> None:
        """Take pieces out of sellable stock: 'damaged' (moved to the damaged bucket) or 'missing'."""
        if self._busy:
            return
        title = tr("Damaged or missing")
        if self._item is None:
            self._error(title, tr("Choose the item first."))
            return
        qty, reason = self.spin_qty.value(), self.txt_adj_reason.text().strip()
        action = stock_ops.mark_damaged if kind == "damaged" else stock_ops.mark_missing
        if reason:
            word = tr("damaged") if kind == "damaged" else tr("missing")
            if not self._ask(title, tr("Record {n} piece(s) of {item} as {word}? They leave sellable stock.",
                                       n=qty, item=self._item["label"], word=word)):
                return
        self._busy = True
        try:
            ok, _ = self._run(
                lambda: action(self.conn, self._item["variant_id"], qty, reason, user_id=self.user_id), title=title)
        finally:
            self._busy = False
        if not ok:
            return
        self.txt_adj_reason.clear()
        self._info(title, tr("Recorded. The pieces are no longer in sellable stock."))
        self.set_item(self._item["variant_id"])
        self.reload_adjustments()

    def _on_damaged(self, *_) -> None:
        self.record_adjustment("damaged")

    def _on_missing(self, *_) -> None:
        self.record_adjustment("missing")