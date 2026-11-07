"""Expenses menu: record a shop expense, find old ones, cancel or correct a mistake, see totals.

No SQL and no rules here. core.expenses checks and saves each action in ONE transaction (expense + payment +
audit entry). An expense is never edited or deleted: a mistake is cancelled (a linked negative entry) or
corrected (cancelled, then the right one added), and the reason and user stay on record.
A message says "saved" only after the core call really returned.
"""

import logging
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLineEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from core import expenses as ec
from core.money import format_rupees, parse_rupees
from core.purchase_service import format_ui_date, parse_ui_date
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Expenses"
log = logging.getLogger("clothshop")

MODE_TEXT = {
    "cash": N_("Cash"), "upi": N_("UPI"), "card": N_("Card"), "bank_transfer": N_("Bank transfer"),
    "cheque": N_("Cheque"), "other": N_("Other"),
}
STATUS_TEXT = {"active": N_("Active"), "reversed": N_("Cancelled"), "reversal": N_("Cancelling entry")}
GROUP_TEXT = {"category": N_("Category"), "day": N_("Day"), "month": N_("Month")}


def _today() -> str:
    return date.today().strftime("%d/%m/%Y")


def _month_start() -> str:
    return date.today().replace(day=1).strftime("%d/%m/%Y")


class ExpensesScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._rows = []            # the expenses shown in the table
        self._summary = []         # the totals shown under it
        self._categories = []      # category names offered in the lists
        self._correcting = None    # id of the expense being corrected, or None
        self._busy = False         # blocks a second click while a save runs

        self.lbl_status = label("", "error")
        self.lbl_mode = label("", "subtitle")

        # ---- the form
        box = QGroupBox(tr("Expense"))
        grid = QGridLayout(box)
        self.txt_date = QLineEdit(_today())
        self.cmb_category = QComboBox()
        self.cmb_category.setEditable(True)
        self.txt_description = QLineEdit()
        self.txt_amount = QLineEdit()
        self.txt_amount.setPlaceholderText(tr("For example 1250.50"))
        self.cmb_mode = QComboBox()
        for key in ec.EXPENSE_MODES:
            self.cmb_mode.addItem(tr(MODE_TEXT[key]), key)
        self.txt_reference = QLineEdit()
        self.txt_notes = QLineEdit()
        self.txt_receipt = QLineEdit()
        self.txt_receipt.setReadOnly(True)
        self.txt_receipt.setPlaceholderText(tr("No receipt file chosen (up to 5 MB)"))
        self.btn_receipt = QPushButton(tr("Choose receipt…"))
        self.btn_clear_receipt = QPushButton(tr("Remove"))
        for r, (text, widget) in enumerate((
                (tr("Date (DD/MM/YYYY)"), self.txt_date), (tr("Category"), self.cmb_category),
                (tr("Description"), self.txt_description), (tr("Amount (₹)"), self.txt_amount),
                (tr("Paid by"), self.cmb_mode), (tr("Reference"), self.txt_reference), (tr("Notes"), self.txt_notes))):
            grid.addWidget(label(text, "subtitle", wrap=False), r // 2, (r % 2) * 2)
            grid.addWidget(widget, r // 2, (r % 2) * 2 + 1)
        grid.addWidget(label(tr("Receipt"), "subtitle", wrap=False), 4, 0)
        receipt = QHBoxLayout()
        receipt.addWidget(self.txt_receipt, 1)
        receipt.addWidget(self.btn_receipt)
        receipt.addWidget(self.btn_clear_receipt)
        grid.addLayout(receipt, 4, 1, 1, 3)
        buttons = QHBoxLayout()
        self.btn_save = QPushButton(tr("Save expense"))
        self.btn_stop = QPushButton(tr("Stop correcting"))
        self.btn_stop.setVisible(False)
        buttons.addWidget(self.btn_save)
        buttons.addWidget(self.btn_stop)
        buttons.addStretch(1)
        grid.addLayout(buttons, 5, 0, 1, 4)

        # ---- find old expenses
        find = QHBoxLayout()
        self.txt_from, self.txt_to = QLineEdit(_month_start()), QLineEdit(_today())
        self.cmb_filter = QComboBox()
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(tr("Search description, reference or notes"))
        self.btn_show = QPushButton(tr("Show"))
        for text, widget in ((tr("From"), self.txt_from), (tr("To"), self.txt_to), (tr("Category"), self.cmb_filter)):
            find.addWidget(label(text, "subtitle", wrap=False))
            find.addWidget(widget)
        find.addWidget(self.txt_search, 1)
        find.addWidget(self.btn_show)

        self.tbl = make_table([tr("Date"), tr("Category"), tr("Description"), tr("Amount"), tr("Paid by"),
                               tr("Reference"), tr("Status")])
        self.lbl_total = label("", "title", wrap=False)
        actions = QHBoxLayout()
        self.btn_correct = QPushButton(tr("Correct the selected expense…"))
        self.btn_cancel = QPushButton(tr("Cancel the selected expense…"))
        actions.addWidget(self.btn_correct)
        actions.addWidget(self.btn_cancel)
        actions.addStretch(1)

        self.cmb_group = QComboBox()
        for key in GROUP_TEXT:
            self.cmb_group.addItem(tr(GROUP_TEXT[key]), key)
        group_row = QHBoxLayout()
        group_row.addWidget(label(tr("Totals for these dates, by"), "subtitle", wrap=False))
        group_row.addWidget(self.cmb_group)
        group_row.addStretch(1)
        self.tbl_summary = make_table([tr("Group"), tr("Total")])
        self.tbl_summary.setMaximumHeight(170)

        col = QVBoxLayout(self)
        col.setContentsMargins(18, 14, 18, 14)
        col.addWidget(label(tr("Expenses"), "title", wrap=False))
        col.addWidget(self.lbl_status)
        col.addWidget(self.lbl_mode)
        col.addWidget(box)
        col.addLayout(find)
        col.addWidget(self.tbl, 1)
        col.addWidget(self.lbl_total)
        col.addLayout(actions)
        col.addLayout(group_row)
        col.addWidget(self.tbl_summary)

        self.btn_save.clicked.connect(self.save_expense)
        self.btn_stop.clicked.connect(self.stop_correcting)
        self.btn_receipt.clicked.connect(self.choose_receipt)
        self.btn_clear_receipt.clicked.connect(lambda *_: self.txt_receipt.clear())
        self.btn_show.clicked.connect(self.reload)
        self.txt_search.returnPressed.connect(self.reload)
        self.btn_correct.clicked.connect(self.start_correction)
        self.btn_cancel.clicked.connect(self.cancel_selected)
        self.cmb_group.currentIndexChanged.connect(self.reload)
        self.reload()

    # ------------------------------------------------------------ small helpers that tests replace

    def _text_dialog(self, title: str, text: str, default: str = ""):
        """One line of text, or None when cancelled."""
        value, accepted = QInputDialog.getText(self, title, text, text=default)
        return value if accepted else None

    def _file_dialog(self) -> str:
        """The receipt file the owner chose, or an empty text."""
        path, _filter = QFileDialog.getOpenFileName(self, tr("Choose receipt"))
        return path or ""

    def choose_receipt(self, *_):
        path = self._file_dialog()
        if path:
            self.txt_receipt.setText(path)

    # ------------------------------------------------------------ loading

    def refresh(self, *_):
        """Called each time the page is opened."""
        self.reload()

    def _lists_of_categories(self, seen):
        names = list(ec.DEFAULT_CATEGORIES)
        for name in seen:
            if name and name.lower() not in (n.lower() for n in names):
                names.append(name)
        return names

    def reload(self, *_):
        uid = self.user_id
        ok, everything = self._run(lambda: ec.list_expenses(self.conn, user_id=uid), title=tr("Expenses"),
                                   quiet_to=self.lbl_status)
        if ok:
            self._set_categories(self._lists_of_categories(r["category"] for r in everything))

        def listing():
            low = parse_ui_date(self.txt_from.text(), "From date") if self.txt_from.text().strip() else None
            high = parse_ui_date(self.txt_to.text(), "To date") if self.txt_to.text().strip() else None
            rows = ec.list_expenses(self.conn, user_id=uid, date_from=low, date_to=high,
                                    category=self.cmb_filter.currentData(), search=self.txt_search.text())
            summary = ec.expense_summary(self.conn, low or "2000-01-01", high or "2999-12-31", user_id=uid,
                                         group_by=self.cmb_group.currentData())
            return rows, summary

        ok, result = self._run(listing, title=tr("Expenses"), quiet_to=self.lbl_status)
        if not ok:
            return
        self._rows, summary = result
        self._summary = summary["rows"]
        self.lbl_status.setText("")
        self.tbl.setRowCount(0)
        for r in self._rows:
            add_row(self.tbl, [format_ui_date(r["expense_date"]), r["category"], r["description"],
                               format_rupees(r["amount_paise"]), tr(MODE_TEXT.get(r["payment_mode"], r["payment_mode"])),
                               r["reference"] or "", tr(STATUS_TEXT[r["status"]])], right=(3,))
        self.lbl_total.setText(tr("Total of the expenses shown: {total}",
                                  total=format_rupees(sum(r["amount_paise"] for r in self._rows))))
        self.tbl_summary.setRowCount(0)
        for g in self._summary:
            key = format_ui_date(g["key"]) if self.cmb_group.currentData() == "day" else g["key"]
            add_row(self.tbl_summary, [key, format_rupees(g["total_paise"])], right=(1,))

    def _set_categories(self, names):
        self._categories = names
        for combo, first in ((self.cmb_category, None), (self.cmb_filter, tr("All categories"))):
            current_text, current_data = combo.currentText(), combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            if first is not None:
                combo.addItem(first, None)
            for name in names:
                combo.addItem(name, name)
            if first is None:
                combo.setCurrentText(current_text if current_text else names[0])
            else:
                index = combo.findData(current_data)
                combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _chosen(self):
        row = self.tbl.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    # ------------------------------------------------------------ saving

    def _clear_form(self):
        self.txt_date.setText(_today())
        self.txt_description.clear()
        self.txt_amount.clear()
        self.txt_reference.clear()
        self.txt_notes.clear()
        self.txt_receipt.clear()
        self.cmb_mode.setCurrentIndex(0)
        self._correcting = None
        self.btn_save.setText(tr("Save expense"))
        self.btn_stop.setVisible(False)
        self.lbl_mode.setText("")

    def save_expense(self, *_):
        if self._busy:
            return
        uid = self.user_id
        text = {
            "category": self.cmb_category.currentText(), "description": self.txt_description.text(),
            "mode": self.cmb_mode.currentData(), "reference": self.txt_reference.text(),
            "notes": self.txt_notes.text(), "receipt": self.txt_receipt.text().strip() or None,
        }
        reason = None
        if self._correcting is not None:
            reason = self._text_dialog(tr("Correct the expense"), tr("Why is this being corrected?"))
            if reason is None:
                return
        self._busy = True
        try:
            if self._correcting is None:
                ok, _ = self._run(lambda: ec.add_expense(
                    self.conn, parse_ui_date(self.txt_date.text(), "Date"), text["category"], text["description"],
                    parse_rupees(self.txt_amount.text(), allow_zero=False), user_id=uid, payment_mode=text["mode"],
                    reference=text["reference"].strip() or None, notes=text["notes"].strip() or None,
                    attachment_path=text["receipt"]), title=tr("Expenses"))
                done = tr("Expense saved.")
            else:
                ok, _ = self._run(lambda: ec.correct_expense(
                    self.conn, self._correcting, reason, user_id=uid,
                    expense_date=parse_ui_date(self.txt_date.text(), "Date"), category=text["category"],
                    description=text["description"], amount_paise=parse_rupees(self.txt_amount.text(), allow_zero=False),
                    payment_mode=text["mode"], reference=text["reference"].strip(), notes=text["notes"].strip(),
                    attachment_path=text["receipt"]), title=tr("Expenses"))
                done = tr("Expense corrected. The old entry is cancelled and kept on record.")
        finally:
            self._busy = False
        if not ok:
            return  # what was typed stays, so it can be fixed
        self._clear_form()
        self.reload()
        self._info(tr("Expenses"), done)

    # ------------------------------------------------------------ cancel and correct

    def cancel_selected(self, *_):
        if self._busy:
            return
        expense = self._chosen()
        if expense is None:
            self._error(tr("Expenses"), tr("Choose an expense in the table first."))
            return
        reason = self._text_dialog(tr("Cancel the expense"), tr("Why is this expense being cancelled?"))
        if reason is None:
            return
        self._busy = True
        try:
            ok, _ = self._run(lambda: ec.reverse_expense(self.conn, expense["id"], reason, user_id=self.user_id),
                              title=tr("Expenses"))
        finally:
            self._busy = False
        if ok:
            if self._correcting == expense["id"]:
                self._clear_form()
            self.reload()
            self._info(tr("Expenses"), tr("The expense is cancelled. A cancelling entry was added; nothing was deleted."))

    def start_correction(self, *_):
        expense = self._chosen()
        if expense is None:
            self._error(tr("Expenses"), tr("Choose an expense in the table first."))
            return
        if expense["status"] != "active":
            self._error(tr("Expenses"), tr("Only an active expense can be corrected."))
            return
        self._correcting = expense["id"]
        self.txt_date.setText(format_ui_date(expense["expense_date"]))
        self.cmb_category.setCurrentText(expense["category"])
        self.txt_description.setText(expense["description"])
        self.txt_amount.setText(f"{expense['amount_paise'] // 100}.{expense['amount_paise'] % 100:02d}")
        index = self.cmb_mode.findData(expense["payment_mode"])
        self.cmb_mode.setCurrentIndex(index if index >= 0 else 0)
        self.txt_reference.setText(expense["reference"] or "")
        self.txt_notes.setText(expense["notes"] or "")
        self.txt_receipt.setText(expense["attachment_path"] or "")
        self.btn_save.setText(tr("Save correction"))
        self.btn_stop.setVisible(True)
        self.lbl_mode.setText(tr("You are correcting an expense. Change what is wrong and press Save correction."))

    def stop_correcting(self, *_):
        self._clear_form()


def build(conn, session, folder):
    return ExpensesScreen(conn, session)
