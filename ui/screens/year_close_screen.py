"""Year Close menu: lock a finished financial year (USR-05) and export vouchers for Tally.

No SQL and no rules here: core.fy_lock and core.tally_export do the work and check the permissions.
"""

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout,
                               QWidget)

from core import fy_lock, tally_export
from core.numbering import financial_year
from ui.i18n import tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Year Close and Tally"


class YearCloseScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.lbl_status = label("", "error")
        self.lbl_done = label("")

        self.year = QLineEdit()
        today = date.today()
        self.year.setPlaceholderText(tr("e.g. {year}", year=_previous_year(today)))
        self.btn_lock = QPushButton(tr("Lock this year"))
        self.btn_unlock = QPushButton(tr("Unlock selected year"))
        self.tbl = make_table([tr("Locked year"), tr("Locked on")])
        lock_row = QHBoxLayout()
        lock_row.addWidget(self.year)
        lock_row.addWidget(self.btn_lock)
        lock_row.addWidget(self.btn_unlock)
        box = QGroupBox(tr("Financial year lock"))
        inner = QVBoxLayout(box)
        inner.addWidget(label(tr("A locked year accepts no new bills, credit notes, purchases or expenses dated in it. "
                                 "Work done today still goes in today's year."), "subtitle"))
        inner.addLayout(lock_row)
        inner.addWidget(self.tbl)

        start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        self.date_from, self.date_to = QLineEdit(start.isoformat()), QLineEdit(today.isoformat())
        self.btn_tally = QPushButton(tr("Save Tally file..."))
        form = QFormLayout()
        form.addRow(tr("From date"), self.date_from)
        form.addRow(tr("To date"), self.date_to)
        form.addRow("", self.btn_tally)
        tally = QGroupBox(tr("Tally export"))
        tally_col = QVBoxLayout(tally)
        tally_col.addWidget(label(tr("Sales, credit notes and purchases of these dates as a Tally XML file. The ledgers "
                                     "(Sales, Cash, Output CGST ...) must already exist in Tally."), "subtitle"))
        tally_col.addLayout(form)

        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(tr("Year Close and Tally"), "title"))
        col.addWidget(box, 1)
        col.addWidget(tally)
        col.addWidget(self.lbl_done)
        col.addWidget(self.lbl_status)

        self.btn_lock.clicked.connect(self.lock)
        self.btn_unlock.clicked.connect(self.unlock)
        self.btn_tally.clicked.connect(self.export)
        self.load()

    def load(self) -> None:
        self.tbl.setRowCount(0)
        self._years = fy_lock.locked_years(self.conn)
        for y in self._years:
            add_row(self.tbl, [y["fin_year"], y["locked_at"]])

    def lock(self) -> None:
        year = self.year.text().strip()
        if not self._ask(tr("Lock this year"), tr("Lock the financial year {year}? Nothing new can be dated in it.", year=year)):
            return
        ok, _ = self._run(lambda: fy_lock.lock_year(self.conn, self.user_id, year), title=tr("Lock this year"))
        if ok:
            self.year.clear()
            self.lbl_done.setText(tr("Financial year {year} is locked.", year=year))
            self.load()

    def unlock(self) -> None:
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            self._error(tr("Unlock selected year"), tr("Select a year first."))
            return
        year = self._years[rows[0].row()]["fin_year"]
        if not self._ask(tr("Unlock selected year"), tr("Unlock the financial year {year}?", year=year)):
            return
        ok, _ = self._run(lambda: fy_lock.unlock_year(self.conn, self.user_id, year, "unlocked from the Year Close screen"),
                          title=tr("Unlock selected year"))
        if ok:
            self.lbl_done.setText(tr("Financial year {year} is unlocked.", year=year))
            self.load()

    def export(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, tr("Save Tally file..."), "tally-vouchers.xml", "XML (*.xml)")
        if not path:
            return
        ok, result = self._run(lambda: tally_export.export_tally_xml(self.conn, self.user_id, self.date_from.text().strip(),
                                                                     self.date_to.text().strip(), path),
                               title=tr("Save Tally file..."))
        if ok:
            message = tr("Saved {sales} sale(s), {notes} credit note(s) and {purchases} purchase(s): {path}",
                         sales=result["sales"], notes=result["credit_notes"], purchases=result["purchases"], path=result["path"])
            self.lbl_done.setText(message)
            self._info(tr("Tally export"), message)


def _previous_year(today) -> str:
    return financial_year(date(today.year - 1 if today.month >= 4 else today.year - 2, 6, 1))


def build(conn, session, folder):
    return YearCloseScreen(conn, session)
