# ui/purchase_returns_view.py
"""Purchase Returns screen: send goods back to a supplier (SRS 13, PUR-06).

This screen only shows things and passes what was typed to core.purchase_service. It never touches
the database itself, so the permission check, the debit note number, the stock reduction, the refund
and the audit entry all stay in ONE tested core transaction.

Rules kept here:
  * "Saved" is shown only after the core call really returned. A failure shows a short message and
    nothing is changed.
  * A second click (or a retry after a fault) cannot return the same goods twice: every attempt carries
    an idempotency key, and the button is blocked while an action is running.
  * The number of pieces that can be returned is limited by what was bought, what was already
    returned and what is in stock now.
"""

import logging
import uuid

from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from core import purchase_service as svc
from core import supplier_admin
from core.money import format_rupees
from ui.i18n import tr
from ui.screen_helpers import MODE_TEXT, ScreenBase, add_row, fill_combo, make_table
from ui.widgets import form, label

log = logging.getLogger("clothshop")

SETTLEMENT_TEXT = {
    "refund": "Supplier refunds the money",
    "credit": "Keep as credit with the supplier",
}


class PurchaseReturnsView(ScreenBase, QWidget):
    """Return goods to a supplier against a confirmed purchase."""

    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self._purchases = []      # rows of the "purchases that can still be returned" list
        self._lines = []          # the lines of the selected purchase
        self._boxes = []          # one spin box per line: pieces to return
        self._busy = False        # blocks a second click while an action runs
        self._key = uuid.uuid4().hex  # one key per return attempt, renewed after a successful return

        self.lbl_status = label("", "error")

        # ---- find the purchase
        find = QHBoxLayout()
        self.cmb_supplier = QComboBox()
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(tr("Search by invoice number, voucher number or supplier"))
        find.addWidget(self.cmb_supplier, 1)
        find.addWidget(self.txt_search, 2)

        self.tbl_purchases = make_table([
            tr("Date"), tr("Supplier"), tr("Invoice number"), tr("Voucher"), tr("Pieces bought"),
            tr("Pieces that can go back"), tr("Invoice total"),
        ])

        # ---- what to return
        box = QGroupBox(tr("Pieces to return"))
        box_col = QVBoxLayout(box)
        self.tbl_lines = make_table([
            tr("Product"), tr("Size"), tr("Colour"), tr("Bought"), tr("Already returned"),
            tr("In stock now"), tr("Cost per piece"), tr("Return pieces"),
        ])
        box_col.addWidget(self.tbl_lines, 1)

        details = form()
        self.cmb_settlement = QComboBox()
        for key in svc.SETTLEMENTS:
            self.cmb_settlement.addItem(tr(SETTLEMENT_TEXT.get(key, key)), key)
        self.cmb_refund_mode = QComboBox()
        self.cmb_refund_mode.addItem(tr("Same as the original payment"), None)
        for mode in svc.PAYMENT_MODES:
            self.cmb_refund_mode.addItem(tr(MODE_TEXT[mode]), mode)
        self.txt_reason = QLineEdit()
        self.txt_reason.setPlaceholderText(tr("Why are the goods going back? (required)"))
        details.addRow(tr("What happens to the money"), self.cmb_settlement)
        details.addRow(tr("Refund method"), self.cmb_refund_mode)
        details.addRow(tr("Reason"), self.txt_reason)
        box_col.addLayout(details)

        self.btn_return = QPushButton(tr("Return goods to supplier"))
        box_col.addWidget(self.btn_return)

        # ---- earlier returns of the selected purchase
        hist = QGroupBox(tr("Earlier returns on this purchase"))
        hist_col = QVBoxLayout(hist)
        self.tbl_history = make_table([
            tr("Debit note"), tr("Date"), tr("Total"), tr("Refunded"), tr("Credit with supplier"), tr("Reason"),
        ])
        hist_col.addWidget(self.tbl_history)

        outer = QVBoxLayout(self)
        outer.addWidget(label(tr("Purchase Returns"), "title"))
        outer.addWidget(self.lbl_status)
        outer.addLayout(find)
        outer.addWidget(self.tbl_purchases, 1)
        outer.addWidget(box, 2)
        outer.addWidget(hist, 1)

        self.cmb_supplier.currentIndexChanged.connect(self.reload_purchases)
        self.txt_search.textChanged.connect(self.reload_purchases)
        self.tbl_purchases.currentCellChanged.connect(self._purchase_selected)
        self.cmb_settlement.currentIndexChanged.connect(self._settlement_changed)
        self.btn_return.clicked.connect(self.return_goods)
        self._settlement_changed()
        self.refresh()

    # ------------------------------------------------------------ loading

    def refresh(self) -> None:
        """Called each time the page is opened."""
        ok, suppliers = self._run(lambda: supplier_admin.list_suppliers(self.conn, self.user_id),
                                  title=tr("Purchase Returns"), quiet_to=self.lbl_status)
        fill_combo(self.cmb_supplier, [(tr("All suppliers"), None)] + [(s["name"], s["id"]) for s in (suppliers or [])])
        self.reload_purchases()

    def _selected_purchase_id(self):
        row = self.tbl_purchases.currentRow()
        if 0 <= row < len(self._purchases):
            return self._purchases[row]["purchase_id"]
        return None

    def reload_purchases(self, *_):
        """Reload the list of purchases and keep the same purchase selected when it is still there."""
        keep = self._selected_purchase_id()
        ok, rows = self._run(
            lambda: svc.find_returnable_purchases(
                self.conn, self.user_id, supplier_id=self.cmb_supplier.currentData(), text=self.txt_search.text()),
            title=tr("Purchase Returns"), quiet_to=self.lbl_status)
        self._purchases = rows or []
        self.tbl_purchases.blockSignals(True)
        self.tbl_purchases.setRowCount(0)
        for r in self._purchases:
            add_row(self.tbl_purchases, [
                svc.format_ui_date(r["invoice_date"]), r["supplier_name"], r["invoice_no"], r["voucher_no"] or "",
                r["bought"], r["returnable"], format_rupees(r["total_paise"]),
            ], right={4, 5, 6})
        index = next((i for i, r in enumerate(self._purchases) if r["purchase_id"] == keep), -1)
        self.tbl_purchases.setCurrentCell(index, 0 if index >= 0 else -1)
        self.tbl_purchases.blockSignals(False)
        self._purchase_selected()

    def _purchase_selected(self, *_):
        purchase_id = self._selected_purchase_id()
        self._lines, self._boxes = [], []
        self.tbl_lines.setRowCount(0)
        self.tbl_history.setRowCount(0)
        if purchase_id is None:
            self._update_button()
            return
        ok, lines = self._run(lambda: svc.returnable_lines(self.conn, self.user_id, purchase_id),
                              title=tr("Purchase Returns"), quiet_to=self.lbl_status)
        self._lines = lines or []
        for line in self._lines:
            row = add_row(self.tbl_lines, [
                line["product_name"], line["size"] or "", line["colour"] or "", line["qty_bought"],
                line["qty_returned"], line["sellable_now"], format_rupees(line["unit_cost_paise"]), "",
            ], right={3, 4, 5, 6})
            spin = QSpinBox()
            spin.setRange(0, line["max_qty"])
            spin.setEnabled(line["max_qty"] > 0)
            self.tbl_lines.setCellWidget(row, 7, spin)
            self._boxes.append(spin)
        ok, history = self._run(lambda: svc.purchase_return_history(self.conn, self.user_id, purchase_id),
                                title=tr("Purchase Returns"), quiet_to=self.lbl_status)
        for h in history or []:
            add_row(self.tbl_history, [
                h["debit_note_no"], svc.format_ui_date(h["date"]) if h["date"] else "",
                format_rupees(h["total_paise"]), format_rupees(h["refund_paise"]),
                format_rupees(h["supplier_credit_paise"]), h["reason"] or "",
            ], right={2, 3, 4})
        self._update_button()

    def _settlement_changed(self, *_):
        """The refund method only matters when the supplier refunds the money."""
        self.cmb_refund_mode.setEnabled(self.cmb_settlement.currentData() == "refund")

    def _update_button(self):
        self.btn_return.setEnabled(bool(self._lines) and not self._busy)

    # ------------------------------------------------------------ the action

    def return_goods(self) -> None:
        """Send the chosen pieces back. Says 'saved' only after the core call really returned."""
        if self._busy:
            return
        purchase_id = self._selected_purchase_id()
        if purchase_id is None:
            self._error(tr("Purchase Returns"), tr("Choose a purchase first."))
            return
        quantities = {line["purchase_line_id"]: box.value() for line, box in zip(self._lines, self._boxes)
                      if box.value() > 0}
        reason = self.txt_reason.text().strip()
        settlement = self.cmb_settlement.currentData()
        refund_mode = self.cmb_refund_mode.currentData() if settlement == "refund" else None

        # The core code gives the exact message when pieces or the reason are missing; only ask to
        # confirm once there is really something to return.
        if quantities and reason:
            question = tr("Return {n} piece(s) to the supplier? Stock will go down and this cannot be undone.")
            if not self._ask(tr("Purchase Returns"), question.replace("{n}", str(sum(quantities.values())))):
                return

        self._busy = True
        self._update_button()
        try:
            ok, result = self._run(
                lambda: svc.return_to_supplier(
                    self.conn, self.user_id, purchase_id, quantities, reason, settlement=settlement,
                    refund_mode=refund_mode, idempotency_key=self._key),
                title=tr("Purchase Returns"))
        finally:
            self._busy = False
        if not ok:
            self._update_button()
            return

        self._key = uuid.uuid4().hex
        self.txt_reason.clear()
        lines = [tr("Debit note") + f" {result['debit_note_no']} " + tr("saved.")]
        lines.append(tr("Total returned") + ": " + format_rupees(result["total_paise"]))
        if result["due_reduced_paise"]:
            lines.append(tr("Reduced what we owe the supplier by") + " " + format_rupees(result["due_reduced_paise"]))
        if result["refund_paise"]:
            lines.append(tr("Refund from the supplier") + ": " + format_rupees(result["refund_paise"]))
        if result["supplier_credit_paise"]:
            lines.append(tr("Credit kept with the supplier") + ": " + format_rupees(result["supplier_credit_paise"]))
        self._info(tr("Purchase Returns"), "\n".join(lines))
        self.reload_purchases() 
        