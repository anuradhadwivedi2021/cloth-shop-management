"""Small pieces shared by the purchase screens: messages, running one core action, tables.

Rules kept here so every screen behaves the same:
  * An error shown to the user is short and has no technical detail (core.purchase_service.safe_message).
    The real error of a fault goes to the log only.
  * An action reports success only when the core call really returned.
  * The three methods _error / _info / _ask are the only places that open a pop-up, so tests can replace them.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMessageBox, QTableWidget, QTableWidgetItem

from core.purchase_service import safe_message
from ui.i18n import N_, tr

log = logging.getLogger("clothshop")

STATUS_TEXT = {"draft": N_("Draft"), "confirmed": N_("Confirmed"), "cancelled": N_("Cancelled")}
MODE_TEXT = {
    "cash": N_("Cash"), "upi": N_("UPI"), "card": N_("Card"), "bank_transfer": N_("Bank transfer"),
    "cheque": N_("Cheque"), "other": N_("Other"),
}


class ScreenBase:
    """Mix-in for a QWidget screen that has self.conn."""

    def _error(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def _run(self, action, *, title: str, quiet_to=None):
        """Run one core action. Returns (True, result) or (False, None).

        The error is shown in a pop-up, or in the label `quiet_to` when one is given (used while loading
        lists, so a failed refresh does not stack pop-ups). Faults are logged, never shown.
        """
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - every failure becomes a short message
            text, expected = safe_message(exc)
            if not expected:
                log.exception("Purchase screen action failed (%s)", title)
            shown = tr(text)
            if quiet_to is not None:
                quiet_to.setText(shown)
            else:
                self._error(title, shown)
            return False, None
        if quiet_to is not None:
            quiet_to.setText("")
        return True, result


def make_table(headers) -> QTableWidget:
    """A read-only table that selects whole rows. `headers` are already translated."""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def add_row(table: QTableWidget, values, right=()) -> int:
    """Append a row of text. Columns listed in `right` (numbers, money) are right-aligned."""
    row = table.rowCount()
    table.insertRow(row)
    for col, text in enumerate(values):
        item = QTableWidgetItem(str(text))
        if col in right:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        table.setItem(row, col, item)
    return row


def fill_combo(combo, entries, *, keep=True) -> None:
    """Replace the entries of a combo box with [(text, data), ...], keeping the chosen data if it still exists."""
    chosen = combo.currentData() if keep else None
    combo.blockSignals(True)
    combo.clear()
    for text, data in entries:
        combo.addItem(text, data)
    index = combo.findData(chosen) if chosen is not None else -1
    combo.setCurrentIndex(index if index >= 0 else 0)
    combo.blockSignals(False)