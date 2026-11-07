# ui/customer_credit_panel.py
"""Credit terms, dues and payments of ONE customer (SRS 11.3, 14.1, CUS-04).

The panel only shows things and hands what was typed to core.customer_credit. The permission checks, the
credit limit, the receipt number, the split of a payment over the oldest bills and the audit entries all
live there, so they hold even if this panel is wrong.

Rules kept here:
  * "Saved" is shown only after the core call really returned.
  * A double click or a retry cannot take the same payment twice: every attempt carries an idempotency
    key and the buttons are blocked while an action runs.
  * A payment is never deleted: a wrong one is reversed with a reason, which adds an opposite receipt.
  * No lambdas are connected to signals (a lambda that holds the screen can crash Qt when it closes).
"""

import logging
import uuid

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QInputDialog, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

from core import customer_credit as cc
from core.money import format_rupees, parse_rupees
from core.permissions import has_permission
from core.purchase_service import format_ui_date, parse_ui_date
from core.settings import get_bool
from ui.i18n import tr
from ui.screen_helpers import MODE_TEXT, ScreenBase, add_row, make_table
from ui.widgets import form, label

log = logging.getLogger("clothshop")


def _plain(paise: int) -> str:
    """Whole rupees and paise as typed in an amount box, e.g. 30000 -> '300.00'."""
    return f"{paise // 100}.{paise % 100:02d}"


class CustomerCreditPanel(ScreenBase, QWidget):
    """Credit terms, open bills, ageing, receiving payments and reversing receipts for one customer."""

    changed = Signal()  # something was saved: the screen around the panel refreshes its summary

    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.can_pay = has_permission(conn, self.user_id, "receive_customer_payment")
        self.can_terms = has_permission(conn, self.user_id, "manage_settings")
        self._customer_id = None
        self._bills = []        # open bills, oldest first
        self._receipts = []     # receipts and reversals, newest first
        self._busy = False
        self._key = uuid.uuid4().hex  # one key per payment attempt, renewed after a successful payment

        self.lbl_status = label("", "error")
        self.lbl_off = label("", "hint")
        self.lbl_summary = label("", "title")
        self.lbl_ageing = label("", "hint")

        # ---- credit terms
        terms = QGroupBox(tr("Credit terms"))
        tf = form()
        self.chk_credit = QCheckBox(tr("This customer may buy on credit"))
        self.txt_limit = QLineEdit()
        self.txt_limit.setPlaceholderText(tr("Credit limit in rupees"))
        self.spin_days = QSpinBox()
        self.spin_days.setRange(0, cc.MAX_CREDIT_DAYS)
        self.spin_days.setValue(30)
        tf.addRow("", self.chk_credit)
        tf.addRow(tr("Credit limit (₹)"), self.txt_limit)
        tf.addRow(tr("Usual days to pay"), self.spin_days)
        tcol = QVBoxLayout(terms)
        tcol.addLayout(tf)
        self.btn_terms = QPushButton(tr("Save credit terms"))
        tcol.addWidget(self.btn_terms)

        # ---- open bills
        self.tbl_bills = make_table([
            tr("Bill"), tr("Bill date"), tr("Pay by"), tr("Bill total"), tr("Paid"), tr("Returned"), tr("Still due"),
            tr("Days overdue"),
        ])
        due_row = QHBoxLayout()
        self.btn_due_date = QPushButton(tr("Change pay-by date"))
        due_row.addWidget(self.btn_due_date)
        due_row.addStretch(1)

        # ---- receiving a payment
        pay = QGroupBox(tr("Receive a payment"))
        pc = QVBoxLayout(pay)
        pc.addWidget(label(tr("Select a bill to pay that bill. With no bill selected, the oldest bills are paid first."),
                           "hint"))
        row = QHBoxLayout()
        self.cmb_mode = QComboBox()
        for mode in cc.RECEIPT_MODES:
            self.cmb_mode.addItem(tr(MODE_TEXT[mode]), mode)
        self.txt_amount = QLineEdit()
        self.txt_amount.setPlaceholderText(tr("Amount received (₹)"))
        self.txt_reference = QLineEdit()
        self.txt_reference.setPlaceholderText(tr("Reference (optional)"))
        self.btn_receive = QPushButton(tr("Receive payment"))
        self.btn_receive.setObjectName("primary")
        for w in (self.cmb_mode, self.txt_amount, self.txt_reference, self.btn_receive):
            row.addWidget(w)
        pc.addLayout(row)

        # ---- receipts
        self.tbl_receipts = make_table([
            tr("Receipt"), tr("Date"), tr("Method"), tr("Amount"), tr("Note"), tr("Status"),
        ])
        rec_row = QHBoxLayout()
        self.btn_reverse = QPushButton(tr("Reverse selected receipt"))
        rec_row.addWidget(self.btn_reverse)
        rec_row.addStretch(1)

        col = QVBoxLayout(self)
        col.addWidget(self.lbl_status)
        col.addWidget(self.lbl_off)
        col.addWidget(self.lbl_summary)
        col.addWidget(self.lbl_ageing)
        col.addWidget(terms)
        col.addWidget(label(tr("Bills with money still due"), "hint"))
        col.addWidget(self.tbl_bills, 2)
        col.addLayout(due_row)
        col.addWidget(pay)
        col.addWidget(label(tr("Payments received"), "hint"))
        col.addWidget(self.tbl_receipts, 1)
        col.addLayout(rec_row)

        self.btn_terms.clicked.connect(self.save_terms)
        self.btn_receive.clicked.connect(self.receive_payment)
        self.btn_due_date.clicked.connect(self.change_due_date)
        self.btn_reverse.clicked.connect(self.reverse_selected)
        self.tbl_bills.currentCellChanged.connect(self._bill_selected)
        self.set_customer(None)

    # ------------------------------------------------------------ small pop-ups (tests replace these)

    def _ask_text(self, title, text):
        value, ok = QInputDialog.getText(self, title, text)
        return value.strip() if ok else None

    # ------------------------------------------------------------ loading

    def set_customer(self, customer_id) -> None:
        self._customer_id = customer_id
        self._key = uuid.uuid4().hex
        self.txt_amount.clear()
        self.txt_reference.clear()
        self.reload()

    def _selected_bill(self):
        row = self.tbl_bills.currentRow()
        return self._bills[row] if 0 <= row < len(self._bills) else None

    def _selected_receipt(self):
        row = self.tbl_receipts.currentRow()
        return self._receipts[row] if 0 <= row < len(self._receipts) else None

    def reload(self) -> None:
        title = tr("Credit and dues")
        self.tbl_bills.blockSignals(True)
        self.tbl_bills.setRowCount(0)
        self.tbl_bills.blockSignals(False)
        self.tbl_receipts.setRowCount(0)
        self._bills, self._receipts = [], []
        self.lbl_off.setText("" if get_bool(self.conn, "credit_sales_enabled") else
                             tr("Credit sales are switched off for the whole shop (Settings), so no new bill can be left unpaid."))
        if self._customer_id is None:
            self.lbl_summary.setText("")
            self.lbl_ageing.setText("")
            self._update_buttons()
            return
        ok, dues = self._run(lambda: cc.customer_dues(self.conn, self._customer_id, user_id=self.user_id),
                             title=title, quiet_to=self.lbl_status)
        if ok:
            self._bills = dues["bills"]
            self.chk_credit.setChecked(dues["credit_enabled"])
            self.txt_limit.setText(_plain(dues["credit_limit_paise"]))
            self.spin_days.setValue(dues["default_credit_days"])
            text = tr("Owes {amount}", amount=format_rupees(dues["outstanding_paise"]))
            if dues["credit_enabled"]:
                text += "   " + tr("Can still take {amount} on credit", amount=format_rupees(dues["available_credit_paise"]))
            if dues["overdue_paise"]:
                text += "   " + tr("Overdue {amount}", amount=format_rupees(dues["overdue_paise"]))
            self.lbl_summary.setText(text)
            ageing = dues["ageing"]
            self.lbl_ageing.setText(tr("Age of dues:  0–30 days {a}   31–60 days {b}   over 60 days {c}",
                                       a=format_rupees(ageing["bucket_0_30_paise"]),
                                       b=format_rupees(ageing["bucket_31_60_paise"]),
                                       c=format_rupees(ageing["bucket_over_60_paise"])))
            for b in self._bills:
                add_row(self.tbl_bills, [
                    b["invoice_no"], format_ui_date(b["bill_date"]), format_ui_date(b["due_date"]),
                    format_rupees(b["total_paise"]), format_rupees(b["paid_paise"]), format_rupees(b["returned_paise"]),
                    format_rupees(b["due_paise"]), b["overdue_days"] or "",
                ], right={3, 4, 5, 6, 7})
        ok, receipts = self._run(lambda: cc.list_receipts(self.conn, self._customer_id, user_id=self.user_id),
                                 title=title, quiet_to=self.lbl_status)
        self._receipts = receipts or []
        for r in self._receipts:
            if r["reversal_of"] is not None:
                status, note = tr("Reversal"), r["reason"] or ""
            elif r["is_reversed"]:
                status, note = tr("Reversed"), ""
            else:
                status, note = tr("Received"), ""
            add_row(self.tbl_receipts, [
                r["receipt_no"], format_ui_date((r["created_at"] or "")[:10]), tr(MODE_TEXT.get(r["mode"], r["mode"])),
                format_rupees(r["amount_paise"]), note, status,
            ], right={3})
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self._customer_id is not None and not self._busy
        self.btn_terms.setEnabled(has and self.can_terms)
        self.btn_terms.setToolTip("" if self.can_terms else tr("Only the owner can change credit terms."))
        self.btn_receive.setEnabled(has and self.can_pay)
        self.btn_due_date.setEnabled(has and self.can_pay)
        self.btn_reverse.setEnabled(has and self.can_pay)
        for w in (self.chk_credit, self.txt_limit, self.spin_days):
            w.setEnabled(has and self.can_terms)

    def _bill_selected(self, *_) -> None:
        bill = self._selected_bill()
        if bill is not None:
            self.txt_amount.setText(_plain(bill["due_paise"]))

    # ------------------------------------------------------------ credit terms

    def save_terms(self, *_) -> None:
        if self._busy or self._customer_id is None:
            return
        title = tr("Credit terms")
        try:
            limit = parse_rupees(self.txt_limit.text() or "0", allow_zero=True)
        except ValueError as exc:
            self._error(title, str(exc))
            return
        self._busy = True
        try:
            ok, _ = self._run(
                lambda: cc.set_credit_terms(self.conn, self._customer_id, user_id=self.user_id,
                                            credit_enabled=self.chk_credit.isChecked(), credit_limit_paise=limit,
                                            default_credit_days=self.spin_days.value()),
                title=title)
        finally:
            self._busy = False
        if ok:
            self._info(title, tr("Credit terms saved."))
            self.reload()
            self.changed.emit()
        else:
            self._update_buttons()

    # ------------------------------------------------------------ receiving a payment

    def receive_payment(self, *_) -> None:
        """Take a payment. Says 'saved' only after the core call really returned."""
        if self._busy or self._customer_id is None:
            return
        title = tr("Receive a payment")
        try:
            amount = parse_rupees(self.txt_amount.text(), allow_zero=False)
        except ValueError as exc:
            self._error(title, str(exc))
            return
        bill = self._selected_bill()
        mode = self.cmb_mode.currentData()
        question = tr("Receive {amount} from the customer?", amount=format_rupees(amount))
        if not self._ask(title, question):
            return
        self._busy = True
        self._update_buttons()
        try:
            ok, receipt_id = self._run(
                lambda: cc.receive_payment(
                    self.conn, amount_paise=amount, mode=mode, user_id=self.user_id, customer_id=self._customer_id,
                    sale_id=bill["sale_id"] if bill else None, reference=self.txt_reference.text().strip() or None,
                    idempotency_key=self._key),
                title=title)
        finally:
            self._busy = False
        if not ok:
            self._update_buttons()
            return
        self._key = uuid.uuid4().hex
        self.txt_amount.clear()
        self.txt_reference.clear()
        ok, receipt = self._run(lambda: cc.get_receipt(self.conn, receipt_id, user_id=self.user_id), title=title,
                                quiet_to=self.lbl_status)
        if ok:
            self._info(title, tr("Receipt {no} saved. The customer still owes {amount}.", no=receipt["receipt_no"],
                                 amount=format_rupees(receipt["customer_outstanding_paise"])))
        self.reload()
        self.changed.emit()

    # ------------------------------------------------------------ due date and reversal

    def change_due_date(self, *_) -> None:
        if self._busy:
            return
        title = tr("Change pay-by date")
        bill = self._selected_bill()
        if bill is None:
            self._error(title, tr("Select a bill first."))
            return
        text = self._ask_text(title, tr("New pay-by date (DD/MM/YYYY)"))
        if text is None:
            return
        try:
            chosen = parse_ui_date(text, tr("Date"))
        except ValueError as exc:
            self._error(title, str(exc))
            return
        reason = self._ask_text(title, tr("Why is the date changing?"))
        if reason is None:
            return
        ok, _ = self._run(lambda: cc.set_due_date(self.conn, bill["sale_id"], chosen, user_id=self.user_id, reason=reason),
                          title=title)
        if ok:
            self._info(title, tr("The pay-by date was changed. The old date stays in the history."))
            self.reload()
            self.changed.emit()

    def reverse_selected(self, *_) -> None:
        """Cancel a wrong receipt. The original stays; an opposite receipt is added."""
        if self._busy:
            return
        title = tr("Reverse a receipt")
        receipt = self._selected_receipt()
        if receipt is None:
            self._error(title, tr("Select a receipt first."))
            return
        if receipt["reversal_of"] is not None or receipt["is_reversed"]:
            self._error(title, tr("This receipt is already reversed, or is a reversal itself."))
            return
        reason = self._ask_text(title, tr("Why is this receipt being reversed?"))
        if reason is None:
            return
        if reason and not self._ask(title, tr("Reverse receipt {no}? The bills will owe the money again.",
                                              no=receipt["receipt_no"])):
            return
        self._busy = True
        try:
            ok, _ = self._run(lambda: cc.reverse_receipt(self.conn, receipt["id"], user_id=self.user_id, reason=reason),
                              title=title)
        finally:
            self._busy = False
        if ok:
            self._info(title, tr("The receipt was reversed. The bills owe the money again."))
            self.reload()
            self.changed.emit()
        else:
            self._update_buttons()