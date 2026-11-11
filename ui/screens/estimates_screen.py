"""Estimates menu: quote prices to a customer, print or save the quote, and turn it into a bill later (BIL-12).

An estimate takes no stock and is not a bill. Converting it makes a normal bill (payment is taken in the usual
payment window). All rules are in core/estimates.py; this screen shows things and passes on what was typed.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from core import bill_preview as bp
from core import estimates as est
from core.money import format_rupees
from core.permissions import has_permission
from ui import print_service
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, fill_combo, make_table
from ui.widgets import label

MENU_TEXT = "Estimates"
log = logging.getLogger("clothshop")
STATUS_TEXT = {"open": N_("Open"), "converted": N_("Made into a bill"), "cancelled": N_("Cancelled"), "expired": N_("Expired")}
FILTERS = [(N_("All"), None), (N_("Open"), "open"), (N_("Expired"), "expired"), (N_("Made into a bill"), "converted"),
           (N_("Cancelled"), "cancelled")]


def _day(iso) -> str:
    y, m, d = str(iso).split("-")
    return f"{d}/{m}/{y}"


class EstimatesScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._cart = []          # [{'variant_id', 'qty', 'label'}]
        self._item = None
        self._rows = []
        self._busy = False
        self.lbl_status = label("", "error")

        # ---- new estimate
        self.btn_choose = QPushButton(tr("Choose item"))
        self.lbl_item = label(tr("No item chosen yet."))
        self.spin_qty = QSpinBox()
        self.spin_qty.setRange(1, 10000)
        self.btn_add = QPushButton(tr("Add to estimate"))
        self.btn_add.setEnabled(False)
        pick = QHBoxLayout()
        for widget in (self.btn_choose, self.lbl_item):
            pick.addWidget(widget, 1 if widget is self.lbl_item else 0)
        pick.addWidget(label(tr("Pieces"), wrap=False))
        pick.addWidget(self.spin_qty)
        pick.addWidget(self.btn_add)
        self.tbl_cart = make_table([tr("Item"), tr("Pieces"), tr("Rate"), tr("Amount")])
        self.btn_remove = QPushButton(tr("Remove the selected line"))
        self.lbl_total = label("", "title")
        self.txt_customer = QLineEdit()
        self.txt_customer.setPlaceholderText(tr("Customer name (optional)"))
        self.spin_days = QSpinBox()
        self.spin_days.setRange(1, est.MAX_VALID_DAYS)
        self.spin_days.setValue(est.DEFAULT_VALID_DAYS)
        self.txt_note = QLineEdit()
        self.txt_note.setPlaceholderText(tr("Note (optional)"))
        self.btn_save = QPushButton(tr("Save estimate"))
        self.btn_save.setObjectName("primary")
        self.btn_save.setEnabled(False)
        details = QHBoxLayout()
        details.addWidget(self.txt_customer, 1)
        details.addWidget(label(tr("Valid for (days)"), wrap=False))
        details.addWidget(self.spin_days)
        details.addWidget(self.txt_note, 1)
        new_page = QWidget()
        col = QVBoxLayout(new_page)
        col.addWidget(label(tr("An estimate is a price quote. It does not take stock and is not a bill."), "hint"))
        col.addLayout(pick)
        col.addWidget(self.tbl_cart, 1)
        row = QHBoxLayout()
        row.addWidget(self.btn_remove)
        row.addStretch(1)
        row.addWidget(self.lbl_total)
        col.addLayout(row)
        col.addLayout(details)
        col.addWidget(self.btn_save)

        # ---- the list
        self.cmb_filter = QComboBox()
        fill_combo(self.cmb_filter, [(tr(text), key) for text, key in FILTERS])
        self.tbl_list = make_table([tr("Estimate no."), tr("Date"), tr("Customer"), tr("Total"), tr("Valid until"), tr("Status")])
        self.btn_print = QPushButton(tr("Print"))
        self.btn_pdf = QPushButton(tr("Save as PDF..."))
        self.btn_convert = QPushButton(tr("Make a bill..."))
        self.btn_convert.setObjectName("primary")
        self.btn_cancel = QPushButton(tr("Cancel this estimate"))
        self.lbl_detail = label("", "hint")
        list_page = QWidget()
        lcol = QVBoxLayout(list_page)
        lcol.addWidget(self.cmb_filter)
        lcol.addWidget(self.tbl_list, 1)
        lcol.addWidget(self.lbl_detail)
        buttons = QHBoxLayout()
        for button in (self.btn_print, self.btn_pdf, self.btn_cancel, self.btn_convert):
            buttons.addWidget(button)
        lcol.addLayout(buttons)

        self.tabs = QTabWidget()
        self.tabs.addTab(new_page, tr("New estimate"))
        self.tabs.addTab(list_page, tr("Estimates"))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.addWidget(label(tr("Estimates"), "title"))
        outer.addWidget(self.lbl_status)
        outer.addWidget(self.tabs, 1)

        self.btn_choose.clicked.connect(self.choose_item)
        self.btn_add.clicked.connect(self.add_line)
        self.btn_remove.clicked.connect(self.remove_line)
        self.btn_save.clicked.connect(self.save_estimate)
        self.cmb_filter.currentIndexChanged.connect(self.reload)
        self.tbl_list.itemSelectionChanged.connect(self._selection_changed)
        self.btn_print.clicked.connect(self.print_selected)
        self.btn_pdf.clicked.connect(self.pdf_selected)
        self.btn_convert.clicked.connect(self.convert_selected)
        self.btn_cancel.clicked.connect(self.cancel_selected)
        self.can_sell = has_permission(conn, self.user_id, "sell")
        self._refresh_cart()
        self.reload()
        self._selection_changed()

    # ------------------------------------------------------------ new estimate

    def choose_item(self):
        from ui.stock_count_view import ItemDialog

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
        self.lbl_item.setText(info["label"])
        self.btn_add.setEnabled(self.can_sell)
        return True

    def add_line(self):
        if not self._item:
            return
        for line in self._cart:
            if line["variant_id"] == self._item["variant_id"]:
                line["qty"] += self.spin_qty.value()
                break
        else:
            self._cart.append({"variant_id": self._item["variant_id"], "qty": self.spin_qty.value(), "label": self._item["label"]})
        self._refresh_cart()

    def remove_line(self):
        row = self.tbl_cart.currentRow()
        if 0 <= row < len(self._cart):
            del self._cart[row]
            self._refresh_cart()

    def _lines(self) -> list:
        return [{"variant_id": l["variant_id"], "qty": l["qty"]} for l in self._cart]

    def _refresh_cart(self):
        self.tbl_cart.setRowCount(0)
        self.lbl_total.setText("")
        self.btn_save.setEnabled(False)
        if not self._cart:
            return
        ok, preview = self._run(lambda: bp.preview_bill(self.conn, self._lines()), title=tr("Estimate"), quiet_to=self.lbl_status)
        if not ok:
            for line in self._cart:
                add_row(self.tbl_cart, [line["label"], line["qty"], "", ""], right=(1,))
            return
        for line in preview["lines"]:
            add_row(self.tbl_cart, [line["label"], line["qty"], format_rupees(line["unit_price_paise"]),
                                    format_rupees(line["line_total_paise"])], right=(1, 2, 3))
        self.lbl_total.setText(tr("Estimate total: {amount}", amount=format_rupees(preview["total_paise"])))
        self.btn_save.setEnabled(self.can_sell)

    def save_estimate(self):
        if self._busy or not self._cart:
            return
        self._busy = True
        try:
            ok, estimate_id = self._run(lambda: est.create_estimate(
                self.conn, self.user_id, self._lines(), customer_name=self.txt_customer.text(),
                note=self.txt_note.text(), valid_days=self.spin_days.value()), title=tr("Save estimate"))
        finally:
            self._busy = False
        if not ok:
            return
        saved = est.get_estimate(self.conn, self.user_id, estimate_id)
        self._cart, self._item = [], None
        self.lbl_item.setText(tr("No item chosen yet."))
        self.btn_add.setEnabled(False)
        self.txt_customer.clear()
        self.txt_note.clear()
        self._refresh_cart()
        self.reload()
        self._info(tr("Save estimate"), tr("Estimate {no} saved. Total {amount}, valid until {day}.",
                                           no=saved["estimate_no"], amount=format_rupees(saved["total_paise"]),
                                           day=_day(saved["valid_until"])))

    # ------------------------------------------------------------ the list

    def reload(self):
        ok, rows = self._run(lambda: est.list_estimates(self.conn, self.user_id, status=self.cmb_filter.currentData()),
                             title=tr("Estimates"), quiet_to=self.lbl_status)
        self._rows = rows if ok else []
        self.tbl_list.setRowCount(0)
        for row in self._rows:
            status = "expired" if row["expired"] else row["status"]
            add_row(self.tbl_list, [row["estimate_no"], _day(str(row["created_at"])[:10]), row["customer_name"] or "",
                                    format_rupees(row["total_paise"]), _day(row["valid_until"]), tr(STATUS_TEXT[status])], right=(3,))
        self._selection_changed()

    def _selected(self):
        row = self.tbl_list.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _selection_changed(self):
        chosen = self._selected()
        usable = chosen is not None and self.can_sell
        self.btn_print.setEnabled(usable)
        self.btn_pdf.setEnabled(usable)
        live = usable and chosen["status"] == "open"
        self.btn_cancel.setEnabled(live)
        self.btn_convert.setEnabled(live and not chosen["expired"])
        self.lbl_detail.setText("")
        if chosen and chosen["status"] == "open":
            ok, detail = self._run(lambda: est.get_estimate(self.conn, self.user_id, chosen["id"]), title=tr("Estimates"),
                                   quiet_to=self.lbl_status)
            if ok and detail["price_changed"]:
                self.lbl_detail.setText(tr("Prices have changed since this estimate: the same items now cost {amount}. "
                                           "A bill uses today's prices.", amount=format_rupees(detail["current_total_paise"])))

    def print_selected(self):
        chosen = self._selected()
        if chosen:
            result = print_service.print_estimate(self.conn, self.session, chosen["id"], parent=self)
            (self._info if result.ok else self._error)(tr("Print"), result.message)

    def pdf_selected(self):
        chosen = self._selected()
        if not chosen:
            return
        path, _filter = QFileDialog.getSaveFileName(self, tr("Save as PDF..."), chosen["estimate_no"].replace("/", "-") + ".pdf",
                                                    "PDF (*.pdf)")
        if path:
            result = print_service.save_estimate_pdf(self.conn, self.session, chosen["id"], path)
            (self._info if result.ok else self._error)(tr("Save as PDF..."), result.message)

    def cancel_selected(self):
        chosen = self._selected()
        if not chosen or not self._ask(tr("Cancel this estimate"), tr("Cancel estimate {no}?", no=chosen["estimate_no"])):
            return
        ok, _ = self._run(lambda: est.cancel_estimate(self.conn, self.user_id, chosen["id"]), title=tr("Cancel this estimate"))
        if ok:
            self.reload()

    def convert_selected(self):
        """Take payment in the usual payment window; the bill is saved only if the whole conversion works."""
        from ui.billing_counter_view import PaymentDialog

        chosen = self._selected()
        if not chosen or self._busy:
            return
        ok, detail = self._run(lambda: est.get_estimate(self.conn, self.user_id, chosen["id"]), title=tr("Make a bill..."))
        if not ok:
            return
        total = detail["current_total_paise"] or detail["total_paise"]
        result_box = {}

        def on_save(payments):
            try:
                result_box["result"] = est.convert_estimate(self.conn, self.user_id, chosen["id"], payments)
            except Exception as exc:  # noqa: BLE001 - shown inside the payment window, nothing was saved
                from core.purchase_service import safe_message

                text, expected = safe_message(exc)
                if not expected:
                    log.exception("Estimates: converting estimate %s failed", chosen["id"])
                return False, tr(text)
            return True, ""

        self._busy = True
        try:
            dialog = PaymentDialog(total, on_save, parent=self)
            dialog.exec()
        finally:
            self._busy = False
        result = result_box.get("result")
        if result:
            self._info(tr("Make a bill..."), tr("Bill {no} saved from the estimate.", no=result["invoice_no"]))
            self.reload()


def build(conn, session, folder):
    return EstimatesScreen(conn, session)
