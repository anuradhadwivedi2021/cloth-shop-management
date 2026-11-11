"""Gift Vouchers menu: sell a voucher, see all vouchers and what is left on them, cancel one (CUS-06).

No SQL and no rules here: core.wallet issues and cancels vouchers (the money taken goes to the cash book) and the
bill uses them. The code is shown after the core call succeeded, so it can be written on the voucher card.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core import wallet
from core.money import format_rupees, parse_rupees
from ui.i18n import tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Gift Vouchers"
MODE_TEXT = {"cash": "Cash", "upi": "UPI", "card": "Card", "bank_transfer": "Bank transfer", "cheque": "Cheque", "other": "Other"}
STATUS_TEXT = {"active": "Active", "fully_redeemed": "Used up", "cancelled": "Cancelled", "expired": "Expired"}


class VouchersScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._rows = []
        self.lbl_status = label("", "error")
        self.lbl_done = label("")

        self.amount, self.expiry, self.code = QLineEdit(), QLineEdit(), QLineEdit()
        self.expiry.setPlaceholderText("YYYY-MM-DD")
        self.code.setPlaceholderText(tr("(made automatically)"))
        self.mode = QComboBox()
        for key, text in MODE_TEXT.items():
            self.mode.addItem(tr(text), key)
        self.btn_issue = QPushButton(tr("Sell voucher"))
        form = QFormLayout()
        form.addRow(tr("Voucher value (Rs)"), self.amount)
        form.addRow(tr("Paid by"), self.mode)
        form.addRow(tr("Valid until (optional)"), self.expiry)
        form.addRow(tr("Own code (optional)"), self.code)
        form.addRow("", self.btn_issue)
        box = QGroupBox(tr("Sell a gift voucher"))
        box.setLayout(form)

        self.tbl = make_table([tr("Code"), tr("Value"), tr("Left"), tr("Valid until"), tr("Status")])
        self.btn_cancel = QPushButton(tr("Cancel selected voucher"))
        self.btn_refresh = QPushButton(tr("Refresh"))
        bar = QHBoxLayout()
        bar.addWidget(self.btn_cancel)
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)

        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(tr("Gift Vouchers"), "title"))
        col.addWidget(label(tr("A voucher is paid for when it is sold and then pays part or all of a later bill. "
                               "The money taken now goes into the cash book."), "subtitle"))
        col.addWidget(box)
        col.addWidget(self.lbl_done)
        col.addWidget(self.lbl_status)
        col.addLayout(bar)
        col.addWidget(self.tbl, 1)

        self.btn_issue.clicked.connect(self.issue)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_refresh.clicked.connect(self.load)
        self.load()

    def load(self) -> None:
        self.tbl.setRowCount(0)
        ok, rows = self._run(lambda: wallet.list_vouchers(self.conn, self.user_id), title=tr("Gift Vouchers"),
                             quiet_to=self.lbl_status)
        self._rows = rows if ok else []
        for v in self._rows:
            status = "expired" if v["expired"] and v["status"] == "active" else v["status"]
            add_row(self.tbl, [v["code"], format_rupees(v["value_paise"]), format_rupees(v["balance_paise"]),
                               v["expires_on"] or "-", tr(STATUS_TEXT.get(status, status))], right=(1, 2))

    def issue(self) -> None:
        def work():
            value = parse_rupees(self.amount.text().strip(), allow_zero=False)
            return wallet.issue_voucher(self.conn, self.user_id, value, pay_mode=self.mode.currentData(),
                                        expires_on=self.expiry.text().strip() or None, code=self.code.text().strip() or None)
        ok, result = self._run(work, title=tr("Sell voucher"))
        if not ok:
            return
        self.amount.clear()
        self.expiry.clear()
        self.code.clear()
        message = tr("Voucher sold. Code: {code}", code=result["code"])
        self.lbl_done.setText(message)
        self._info(tr("Sell voucher"), message)
        self.load()

    def cancel(self) -> None:
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            self._error(tr("Cancel selected voucher"), tr("Select a voucher first."))
            return
        voucher = self._rows[rows[0].row()]
        if not self._ask(tr("Cancel selected voucher"), tr("Cancel voucher {code}? It can no longer be used.", code=voucher["code"])):
            return
        ok, _ = self._run(lambda: wallet.cancel_voucher(self.conn, self.user_id, voucher["code"], reason="cancelled at the counter"),
                          title=tr("Cancel selected voucher"))
        if ok:
            self.load()


def build(conn, session, folder):
    return VouchersScreen(conn, session)
