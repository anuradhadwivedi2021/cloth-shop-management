"""Payments and Cash menu: open the day's cash register, record cash put in or taken out, close the day.

No SQL and no rules here. core.cash works out the expected cash from saved records and checks and saves
every action in ONE transaction (with an audit entry). This screen only shows those figures and passes what
the user typed. A message says "saved" or "closed" only after the core call really returned.

What the owner sees:
  * the cash day being worked on (the one still open, otherwise today) and its figures,
  * Open the cash day (opening cash is the cash counted at the last close, unless changed),
  * Cash in / Cash out by hand, always with a reason,
  * Close the day: counted cash, the difference at once, a reason if there is a difference,
    and a confirmation, because a closed day can never be changed,
  * the last days, to look at earlier closings.

A day that was never closed blocks opening the next one; the screen says so plainly instead of failing.
The difference is shown and explained, never used to change any figure.
"""

import logging
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core import cash as cc
from core.money import format_rupees, parse_rupees
from core.permissions import has_permission
from core.purchase_service import format_ui_date
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Payments and Cash"
log = logging.getLogger("clothshop")

HISTORY_DAYS_SHOWN = 60
STATUS_TEXT = {"not_opened": N_("Not opened"), "open": N_("Open"), "closed": N_("Closed")}
DIRECTION_TEXT = {"in": N_("Cash in (put into the drawer)"), "out": N_("Cash out (taken from the drawer)")}


def _today() -> str:
    return date.today().isoformat()


def _plain(paise: int) -> str:
    """Paise as a number the user can edit: 123450 -> '1234.50'."""
    return f"{paise // 100}.{paise % 100:02d}"


class CashScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._can_manage = has_permission(conn, self.user_id, "manage_cash")
        self._chosen_day = None    # a day picked from the history, or None for "the open day, else today"
        self._day = _today()       # the cash day being shown (YYYY-MM-DD)
        self._open_day = None      # the day that is still open, if any
        self._data = None          # core.cash.get_cash_day() of the shown day
        self._history = []         # the days listed in the history table
        self._busy = False         # blocks a second click while a save runs

        self.lbl_status = label("", "error")      # load problems
        self.lbl_day = label("", "title", wrap=False)
        self.lbl_note = label("", "subtitle")
        self.lbl_warning = label("", "error")

        # ---- figures of the shown day
        self.tbl_figures = make_table([tr("Item"), tr("Amount")])
        self.btn_current = QPushButton(tr("Show the current cash day"))
        self.btn_current.setVisible(False)

        self.tbl_moves = make_table([tr("Time"), tr("Type"), tr("Amount"), tr("Reason"), tr("By")])

        # ---- open the day
        self.box_open = QGroupBox(tr("Open the cash register"))
        self.txt_opening = QLineEdit()
        self.txt_opening.setPlaceholderText(tr("For example 2000.00"))
        self.btn_open = QPushButton(tr("Open the cash day"))
        row = QHBoxLayout(self.box_open)
        row.addWidget(label(tr("Opening cash (₹)"), "subtitle", wrap=False))
        row.addWidget(self.txt_opening, 1)
        row.addWidget(self.btn_open)

        # ---- cash in / cash out by hand
        self.box_move = QGroupBox(tr("Cash in or out by hand"))
        self.cmb_direction = QComboBox()
        for key in DIRECTION_TEXT:
            self.cmb_direction.addItem(tr(DIRECTION_TEXT[key]), key)
        self.txt_move_amount = QLineEdit()
        self.txt_move_amount.setPlaceholderText(tr("Amount (₹)"))
        self.txt_move_reason = QLineEdit()
        self.txt_move_reason.setPlaceholderText(tr("Reason, for example small change from the bank"))
        self.btn_move = QPushButton(tr("Save cash movement"))
        move = QVBoxLayout(self.box_move)
        first = QHBoxLayout()
        first.addWidget(self.cmb_direction, 1)
        first.addWidget(self.txt_move_amount, 1)
        move.addLayout(first)
        move.addWidget(self.txt_move_reason)
        move.addWidget(self.btn_move)

        # ---- close the day
        self.box_close = QGroupBox(tr("Close the day"))
        self.txt_counted = QLineEdit()
        self.txt_counted.setPlaceholderText(tr("Count the cash in the drawer and type the total (₹)"))
        self.lbl_difference = label("", "subtitle")
        self.txt_close_reason = QLineEdit()
        self.txt_close_reason.setPlaceholderText(tr("Reason for the difference (needed only if there is one)"))
        self.btn_close = QPushButton(tr("Close the cash day…"))
        close = QVBoxLayout(self.box_close)
        close.addWidget(self.txt_counted)
        close.addWidget(self.lbl_difference)
        close.addWidget(self.txt_close_reason)
        close.addWidget(self.btn_close)

        # ---- earlier days
        self.tbl_history = make_table([tr("Date"), tr("Status"), tr("Opening"), tr("Expected"), tr("Counted"),
                                       tr("Difference"), tr("Reason")])
        self.tbl_history.setMaximumHeight(170)

        left = QVBoxLayout()
        left.addWidget(self.tbl_figures)
        left.addWidget(label(tr("Cash put in or taken out by hand on this day"), "subtitle", wrap=False))
        left.addWidget(self.tbl_moves, 1)
        right = QVBoxLayout()
        right.addWidget(self.box_open)
        right.addWidget(self.box_move)
        right.addWidget(self.box_close)
        right.addStretch(1)
        columns = QHBoxLayout()
        columns.addLayout(left, 3)
        columns.addLayout(right, 2)

        col = QVBoxLayout(self)
        col.setContentsMargins(18, 14, 18, 14)
        col.addWidget(label(tr("Payments and Cash"), "title", wrap=False))
        col.addWidget(self.lbl_status)
        col.addWidget(self.lbl_day)
        col.addWidget(self.lbl_note)
        col.addWidget(self.lbl_warning)
        col.addWidget(self.btn_current)
        col.addLayout(columns, 1)
        col.addWidget(label(tr("Earlier cash days (click one to look at it)"), "subtitle", wrap=False))
        col.addWidget(self.tbl_history)

        self.btn_open.clicked.connect(self.open_day)
        self.btn_move.clicked.connect(self.save_movement)
        self.btn_close.clicked.connect(self.close_day)
        self.btn_current.clicked.connect(self.show_current_day)
        self.txt_counted.textChanged.connect(self._update_difference)
        self.tbl_history.cellClicked.connect(lambda row, _col: self.show_history_day(row))
        self.reload()

    # ------------------------------------------------------------ loading

    def refresh(self, *_):
        """Called each time the page is opened."""
        self.reload()

    def reload(self, *_):
        uid = self.user_id

        def load():
            open_day = cc.get_open_day(self.conn, user_id=uid)
            day = self._chosen_day or open_day or _today()
            return (
                open_day, day,
                cc.get_cash_day(self.conn, day, user_id=uid),
                cc.list_cash_movements(self.conn, day, user_id=uid),
                cc.list_cash_days(self.conn, user_id=uid)[:HISTORY_DAYS_SHOWN],
                cc.suggested_opening(self.conn, day, user_id=uid),
            )

        ok, result = self._run(load, title=tr("Payments and Cash"), quiet_to=self.lbl_status)
        if not ok:
            return
        self._open_day, self._day, self._data, moves, self._history, carried = result
        self._show(moves, carried)

    def _show(self, moves, carried):
        data, day = self._data, self._day
        status = data["status"]
        today = _today()
        self.lbl_day.setText(tr("Cash day {date}: {status}", date=format_ui_date(day),
                                status=tr(STATUS_TEXT[status])))
        self.btn_current.setVisible(self._chosen_day is not None)

        # what the screen tells the user about this day
        notes, warnings = [], []
        if not self._can_manage:
            notes.append(tr("You can look at the cash register but not change it."))
        if status == "not_opened":
            notes.append(tr("The cash register for today is not open yet. Open it with the cash that is in the drawer."))
        elif status == "closed":
            notes.append(tr("This cash day is closed. Its figures cannot be changed."))
            if data.get("changed_after_close"):
                warnings.append(tr(
                    "Cash payments dated into this day were saved after it was closed. When it was closed the "
                    "expected cash was {then}; the saved records now give {now}. The closed figures were not changed.",
                    then=format_rupees(data["closed_expected_paise"]), now=format_rupees(data["expected_paise"])))
        if self._open_day and self._open_day != today and self._chosen_day is None:
            warnings.append(tr(
                "The cash day of {date} was never closed. Close it first; today's register can be opened after that.",
                date=format_ui_date(self._open_day)))
        self.lbl_note.setText(" ".join(notes))
        self.lbl_warning.setText(" ".join(warnings))
        self.lbl_note.setVisible(bool(notes))
        self.lbl_warning.setVisible(bool(warnings))

        # which actions are possible now
        is_open = status == "open"
        self.box_open.setVisible(self._can_manage and status == "not_opened")
        self.box_move.setEnabled(self._can_manage and is_open)
        self.box_close.setEnabled(self._can_manage and is_open)
        if status == "not_opened" and not self.txt_opening.isModified():
            self.txt_opening.setText(_plain(carried))

        self._fill_figures()
        self.tbl_moves.setRowCount(0)
        for m in moves:
            add_row(self.tbl_moves, [
                (m["created_at"] or "")[11:16], tr(DIRECTION_TEXT["in" if m["amount_paise"] > 0 else "out"]),
                format_rupees(abs(m["amount_paise"])), m["reason"], m["user"] or ""], right=(2,))
        self.tbl_history.setRowCount(0)
        for h in self._history:
            closed = h["status"] == "closed"
            add_row(self.tbl_history, [
                format_ui_date(h["business_date"]), tr(STATUS_TEXT[h["status"]]), format_rupees(h["opening_paise"]),
                format_rupees(h["expected_paise"]) if closed else "", format_rupees(h["counted_paise"]) if closed else "",
                format_rupees(h["difference_paise"]) if closed else "", h["difference_reason"] or ""], right=(2, 3, 4, 5))
        self._update_difference()

    def _fill_figures(self):
        d = self._data
        self.tbl_figures.setRowCount(0)
        rows = [
            (tr("Opening cash"), d["opening_paise"]),
            (tr("+ Cash received on bills"), d["cash_sales_paise"]),
            (tr("- Cash refunded to customers"), d["customer_refunds_paise"]),
            (tr("- Cash paid to suppliers"), d["supplier_payments_paise"]),
            (tr("+ Cash refunded by suppliers"), d["supplier_refunds_paise"]),
            (tr("- Cash paid for expenses"), d["expenses_paise"]),
            (tr("+ Cash put in by hand"), d["manual_in_paise"]),
            (tr("- Cash taken out by hand"), d["manual_out_paise"]),
            (tr("Expected cash in the drawer"), d["expected_paise"]),
        ]
        if d["status"] == "closed":
            rows += [(tr("Cash counted at closing"), d["counted_paise"]),
                     (tr("Difference (counted - expected)"), d["difference_paise"])]
        for text, paise in rows:
            add_row(self.tbl_figures, [text, format_rupees(paise)], right=(1,))
        if d["status"] == "closed" and d.get("difference_reason"):
            add_row(self.tbl_figures, [tr("Reason for the difference"), d["difference_reason"]])
        # every row stays visible: no scrolling to find the expected cash
        rows_height = sum(self.tbl_figures.rowHeight(r) for r in range(self.tbl_figures.rowCount()))
        height = max(self.tbl_figures.horizontalHeader().height(), 28) + rows_height + 6
        self.tbl_figures.setMinimumHeight(height)
        self.tbl_figures.setMaximumHeight(height)

    # ------------------------------------------------------------ looking at other days

    def show_history_day(self, row):
        if 0 <= row < len(self._history):
            self._chosen_day = self._history[row]["business_date"]
            self.reload()

    def show_current_day(self, *_):
        self._chosen_day = None
        self.reload()

    # ------------------------------------------------------------ the difference while typing

    def _counted_paise(self):
        """The counted cash typed so far in paise, or None when it is empty or not a valid amount."""
        try:
            return parse_rupees(self.txt_counted.text())
        except ValueError:
            return None

    def _update_difference(self, *_):
        counted = self._counted_paise()
        if counted is None or self._data is None or self._data["status"] != "open":
            self.lbl_difference.setText("")
            return
        diff = counted - self._data["expected_paise"]
        if diff == 0:
            self.lbl_difference.setText(tr("The counted cash matches the expected cash."))
        elif diff < 0:
            self.lbl_difference.setText(tr("Cash is SHORT by {amount}. A reason is needed to close the day.",
                                           amount=format_rupees(-diff)))
        else:
            self.lbl_difference.setText(tr("Cash is MORE than expected by {amount}. A reason is needed to close the day.",
                                           amount=format_rupees(diff)))

    # ------------------------------------------------------------ open the day

    def open_day(self, *_):
        if self._busy:
            return
        text = self.txt_opening.text().strip()
        self._busy = True
        try:
            ok, _id = self._run(lambda: cc.open_cash_day(
                self.conn, self._day, user_id=self.user_id,
                opening_paise=parse_rupees(text) if text else None), title=tr("Payments and Cash"))
        finally:
            self._busy = False
        if not ok:
            return  # what was typed stays, so it can be fixed
        self.txt_opening.clear()
        self.reload()
        self._info(tr("Payments and Cash"), tr("The cash register is open."))

    # ------------------------------------------------------------ cash in / cash out by hand

    def save_movement(self, *_):
        if self._busy or self._data is None:
            return
        direction = self.cmb_direction.currentData()
        self._busy = True
        try:
            ok, _id = self._run(lambda: cc.add_cash_movement(
                self.conn, self._day,
                parse_rupees(self.txt_move_amount.text(), allow_zero=False) * (1 if direction == "in" else -1),
                self.txt_move_reason.text(), user_id=self.user_id), title=tr("Payments and Cash"))
        finally:
            self._busy = False
        if not ok:
            return
        self.txt_move_amount.clear()
        self.txt_move_reason.clear()
        self.reload()
        self._info(tr("Payments and Cash"), tr("The cash movement is saved."))

    # ------------------------------------------------------------ close the day

    def close_day(self, *_):
        if self._busy or self._data is None:
            return
        title = tr("Payments and Cash")
        ok, counted = self._run(lambda: parse_rupees(self.txt_counted.text()), title=title)
        if not ok:
            return
        # Sales or payments may have been saved since this page was drawn: never close on old figures.
        ok, fresh = self._run(lambda: cc.get_cash_day(self.conn, self._day, user_id=self.user_id), title=title)
        if not ok:
            return
        if fresh["expected_paise"] != self._data["expected_paise"]:
            self.reload()
            self._error(title, tr("The cash figures changed since this page was shown. They are updated now. "
                                  "Check the counted cash and press Close again."))
            return
        expected = fresh["expected_paise"]
        difference = counted - expected
        reason = self.txt_close_reason.text().strip()
        if difference != 0 and not reason:
            self._error(title, tr("The counted cash differs from the expected cash by {amount}. "
                                  "Enter a reason to close the day.", amount=format_rupees(abs(difference))))
            return
        if not self._ask(title, tr(
                "Close the cash day of {date}?\n\nExpected cash: {expected}\nCounted cash: {counted}\n"
                "Difference: {difference}\n\nA closed day cannot be changed.",
                date=format_ui_date(self._day), expected=format_rupees(expected), counted=format_rupees(counted),
                difference=format_rupees(difference))):
            return
        self._busy = True
        try:
            ok, closed = self._run(lambda: cc.close_cash_day(
                self.conn, self._day, counted, user_id=self.user_id, reason=reason or None), title=title)
        finally:
            self._busy = False
        if not ok:
            return
        self.txt_counted.clear()
        self.txt_close_reason.clear()
        self.reload()
        self._info(title, tr("The cash day is closed. Expected {expected}, counted {counted}, difference {difference}.",
                             expected=format_rupees(closed["expected_paise"]),
                             counted=format_rupees(closed["counted_paise"]),
                             difference=format_rupees(closed["difference_paise"])))


def build(conn, session, folder):
    return CashScreen(conn, session)
