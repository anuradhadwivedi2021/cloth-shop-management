"""The Dashboard: today's figures, taken only from the tested report functions in core/.

Nothing here does its own SQL or arithmetic on money: every figure comes from core.reports_sales,
core.reports_stock and core.backup, so it always agrees with the reports, bills and stock ledger.
Who sees what follows the same permissions as the reports themselves.
"""

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from core import backup
from core.money import format_rupees
from core.permissions import has_permission
from core.reports_sales import payment_mode_summary, sales_summary
from core.reports_stock import low_stock
from ui.i18n import N_, tr
from ui.widgets import label

MODE_NAMES = {
    "cash": N_("Cash"), "upi": N_("UPI"), "card": N_("Card"), "bank_transfer": N_("Bank transfer"),
    "cheque": N_("Cheque"), "store_credit": N_("Store credit"), "voucher": N_("Voucher"), "other": N_("Other"),
}


def _card(caption: str):
    frame = QFrame()
    frame.setObjectName("card")
    col = QVBoxLayout(frame)
    col.setContentsMargins(18, 14, 18, 14)
    col.addWidget(label(tr(caption), "hint", wrap=False))
    value = label("-", "title", wrap=False)
    note = label("", "hint")
    col.addWidget(value)
    col.addWidget(note)
    col.addStretch(1)
    return frame, value, note


class DashboardPage(QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session = conn, session
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        head = QHBoxLayout()
        self.title = label(tr("Dashboard"), "title", wrap=False)
        self.subtitle = label("", "subtitle", wrap=False)
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(self.refresh)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(refresh)
        outer.addLayout(head)
        outer.addWidget(self.subtitle)

        self.message = label("", "hint")
        outer.addWidget(self.message)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.sales_card, self.sales_value, self.sales_note = _card(N_("Today's sales (invoice total)"))
        self.bills_card, self.bills_value, self.bills_note = _card(N_("Bills today"))
        self.returns_card, self.returns_value, self.returns_note = _card(N_("Returns and exchanges today"))
        self.money_card, self.money_value, self.money_note = _card(N_("Money received today"))
        self.stock_card, self.stock_value, self.stock_note = _card(N_("Low-stock items"))
        self.backup_card, self.backup_value, self.backup_note = _card(N_("Last backup"))
        self.report_cards = [self.sales_card, self.bills_card, self.returns_card, self.money_card, self.stock_card]
        for position, card in enumerate(self.report_cards + [self.backup_card]):
            grid.addWidget(card, position // 3, position % 3)
        outer.addLayout(grid)
        outer.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        uid = self.session.user_id
        today = date.today()
        self.subtitle.setText(tr("Today: {date}", date=today.strftime("%d/%m/%Y")))
        self.message.setText("")
        self.message.setObjectName("hint")

        can_report = has_permission(self.conn, uid, "view_reports")
        for card in self.report_cards:
            card.setVisible(can_report)
        if can_report:
            try:
                self._fill_reports(uid, today.isoformat())
            except Exception:  # a broken figure must never take the whole screen down
                self.message.setText(tr("Some figures could not be loaded. Please tell the owner."))
                self.message.setObjectName("error")
                self.message.style().unpolish(self.message)
                self.message.style().polish(self.message)
        else:
            self.message.setText(tr("Your role does not show sales figures. Use the menu on the left to start work."))

        can_backup = has_permission(self.conn, uid, "create_backup")
        self.backup_card.setVisible(can_backup)
        if can_backup:
            due = backup.backup_due(self.conn)
            if due["last_backup_at"] is None:
                self.backup_value.setText(tr("Never"))
                self.backup_note.setText(tr("No backup yet. Open Backup and Restore."))
            else:
                self.backup_value.setText(tr("{days} day(s) ago", days=due["days_since"]))
                text = tr("On {when}.", when=due["last_backup_at"])
                self.backup_note.setText(text + (" " + tr("Time for a new backup.") if due["due"] else ""))

    def _fill_reports(self, uid: int, day: str) -> None:
        s = sales_summary(self.conn, day, day, user_id=uid)
        self.sales_value.setText(format_rupees(s["invoice_total_paise"]))
        self.sales_note.setText(tr("Net sales after returns: {amount} (before tax)", amount=format_rupees(s["net_sales_paise"])))
        self.bills_value.setText(str(s["bill_count"]))
        self.bills_note.setText(tr("{n} item(s) sold", n=s["items_sold"]))
        self.returns_value.setText(str(s["credit_note_count"]))
        self.returns_note.setText(tr("Exchanges: {count}. Refunds: {amount}", count=s["exchange_count"], amount=format_rupees(s["refunds_paise"])))

        modes = payment_mode_summary(self.conn, day, day, user_id=uid)
        self.money_value.setText(format_rupees(sum(m["net_paise"] for m in modes)))
        self.money_note.setText(
            ", ".join(f"{tr(MODE_NAMES.get(m['mode'], m['mode']))} {format_rupees(m['net_paise'])}" for m in modes)
            or tr("Nothing received yet")
        )

        low = low_stock(self.conn, user_id=uid)
        self.stock_value.setText(str(len(low)))
        self.stock_note.setText(
            tr("Everything is above its reorder level.") if not low
            else tr("Most urgent: {names}", names=", ".join(f"{r['product']} {r['size']}/{r['colour']}" for r in low[:2]))
        )
