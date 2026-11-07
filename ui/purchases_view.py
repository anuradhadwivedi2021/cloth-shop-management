# ui/purchases_view.py
"""Purchases screen: enter a supplier invoice (size x colour grid), review, save as draft or confirm,
cancel a draft, and pay the supplier.

This screen only shows things and passes what was typed to core.purchase_service and
core.supplier_ledger. It never touches the database itself, so permissions, the audit log, one
transaction per action and the "stock changes only when a purchase is confirmed" rule stay in the
tested core code. A message says "saved" only after the core call has really returned.
"""

import logging
import uuid
from datetime import date

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from core import purchase_service as svc
from core import supplier_admin, supplier_ledger
from core.money import format_rupees, parse_rupees
from core.permissions import has_permission
from ui import print_service
from ui.i18n import tr
from ui.screen_helpers import MODE_TEXT, STATUS_TEXT, ScreenBase, add_row, fill_combo, make_table
from ui.widgets import form, label

log = logging.getLogger("clothshop")


def today_text() -> str:
    return date.today().strftime("%d/%m/%Y")


def rate_text(rate) -> str:
    return "" if rate is None else f"{float(rate):g}"


def totals_rows(summary) -> list:
    """[(label, money text)] for the invoice totals, shared by the form and the review window."""
    return [
        (tr("Subtotal (before GST)"), format_rupees(summary["subtotal_paise"])),
        (tr("GST"), format_rupees(summary["tax_paise"])),
        (tr("Discount"), format_rupees(summary["discount_paise"])),
        (tr("Other charges"), format_rupees(summary["freight_paise"])),
        (tr("Round-off"), format_rupees(summary["round_off_paise"])),
        (tr("Invoice total"), format_rupees(summary["total_paise"])),
        (tr("Paid now"), format_rupees(summary["paid_paise"])),
        (tr("Still to pay"), format_rupees(summary["due_paise"])),
    ]


# ---------------------------------------------------------------- the size x colour grid window

class GridDialog(QDialog):
    """One product: a box for every size and colour. Ask for the quantity of each, the cost and the GST rate."""

    def __init__(self, matrix, parent=None):
        super().__init__(parent)
        self.matrix = matrix
        self.lines = None
        self.setWindowTitle(tr("Sizes and colours: {name}", name=matrix["product"]["name"]))
        self.resize(720, 520)
        self.boxes = {}  # variant_id -> QSpinBox
        by_pair = {(c["size"], c["colour"]): c for c in matrix["cells"]}

        col = QVBoxLayout(self)
        col.addWidget(label(tr("Type how many pieces came for each size and colour. Leave 0 for none."), "subtitle"))
        self.table = make_table([tr("Size")] + list(matrix["colours"]))
        for size in matrix["sizes"]:
            row = add_row(self.table, [size] + [""] * len(matrix["colours"]))
            for index, colour in enumerate(matrix["colours"], start=1):
                cell = by_pair.get((size, colour))
                if cell is None:
                    continue  # this size/colour has no variant: it stays empty
                box = QSpinBox()
                box.setRange(0, svc.MAX_QTY)
                box.setToolTip(tr("In stock now: {qty}", qty=cell["stock"]))
                box.valueChanged.connect(self._update_total)
                self.table.setCellWidget(row, index, box)
                self.boxes[cell["variant_id"]] = box
        col.addWidget(self.table, 1)

        fields = form()
        product = matrix["product"]
        self.txt_cost = QLineEdit(format_rupees(product["default_cost_paise"]) if product["default_cost_paise"] else "")
        self.txt_cost.setPlaceholderText(tr("Cost per piece, for example 500.00"))
        self.txt_gst = QLineEdit(rate_text(product["gst_rate_percent"]))
        self.txt_gst.setPlaceholderText(tr("GST % on this invoice, for example 5"))
        fields.addRow(tr("Cost per piece (₹)"), self.txt_cost)
        fields.addRow(tr("GST %"), self.txt_gst)
        col.addLayout(fields)
        self.lbl_total = label("", "subtitle")
        self.lbl_error = label("", "error")
        col.addWidget(self.lbl_total)
        col.addWidget(self.lbl_error)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton(tr("Cancel"))
        self.btn_ok = QPushButton(tr("Add to invoice"))
        self.btn_ok.setObjectName("primary")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok.clicked.connect(self.add_to_invoice)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_ok)
        col.addLayout(row)
        self._update_total()

    def _update_total(self, *_):
        self.lbl_total.setText(tr("Total pieces: {qty}", qty=sum(b.value() for b in self.boxes.values())))

    def add_to_invoice(self):
        try:
            cost_text = self.txt_cost.text().strip()
            if not cost_text:
                raise ValueError("Enter the purchase cost per piece.")
            lines = svc.lines_from_matrix(
                self.matrix, {vid: box.value() for vid, box in self.boxes.items()},
                unit_cost_paise=parse_rupees(cost_text), tax_rate_percent=svc.parse_tax_rate(self.txt_gst.text()),
            )
        except ValueError as exc:
            self.lbl_error.setText(tr(str(exc)))
            return
        self.lines = lines
        self.accept()


# ---------------------------------------------------------------- the review window

class ReviewDialog(QDialog):
    """The summary shown before anything is saved. `choice` is 'draft', 'confirm' or None (back to edit)."""

    def __init__(self, summary, header_text, parent=None):
        super().__init__(parent)
        self.choice = None
        self.setWindowTitle(tr("Review purchase"))
        self.resize(780, 560)
        col = QVBoxLayout(self)
        col.addWidget(label(header_text))
        self.table = make_table([tr("Product"), tr("Size"), tr("Colour"), tr("Pieces"), tr("Cost per piece"),
                                 tr("GST %"), tr("Line total")])
        for l in summary["lines"]:
            add_row(self.table, [l["product"], l["size"], l["colour"], l["qty"], format_rupees(l["unit_cost_paise"]),
                                 rate_text(l["tax_rate_percent"]), format_rupees(l["line_total_paise"])],
                    right={3, 4, 5, 6})
        col.addWidget(self.table, 1)
        col.addWidget(label(tr("Pieces: {qty}", qty=summary["total_qty"]), "subtitle"))
        col.addWidget(label("\n".join(f"{name}: {value}" for name, value in totals_rows(summary))))
        col.addWidget(label(tr("Stock goes up only when you press 'Confirm and receive stock'. "
                               "A draft does not change stock."), "hint"))
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_back = QPushButton(tr("Back to edit"))
        self.btn_draft = QPushButton(tr("Save as draft"))
        self.btn_confirm = QPushButton(tr("Confirm and receive stock"))
        self.btn_confirm.setObjectName("primary")
        self.btn_back.clicked.connect(self.reject)
        self.btn_draft.clicked.connect(lambda: self._choose("draft"))
        self.btn_confirm.clicked.connect(lambda: self._choose("confirm"))
        for button in (self.btn_back, self.btn_draft, self.btn_confirm):
            row.addWidget(button)
        col.addLayout(row)

    def _choose(self, choice):
        self.choice = choice
        self.accept()


# ---------------------------------------------------------------- the screen

class PurchasesView(ScreenBase, QWidget):
    """Receive stock from a supplier: draft, review, confirm, cancel, and pay the supplier."""

    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.can_pay = has_permission(conn, self.user_id, "manage_cash")
        self.can_labels = has_permission(conn, self.user_id, "print_labels")
        self._lines = []          # the lines of the invoice being typed
        self._summary = None      # the last worked-out invoice (None when it cannot be worked out)
        self._editing_id = None   # the draft being edited; saving replaces it
        self._busy = False        # blocks a second click while an action runs
        self._purchases = []      # rows of the Purchases list
        self._bills = []          # rows of the supplier's unpaid bills
        self._pay_key = uuid.uuid4().hex

        self.lbl_status = label("", "error")
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_new_tab(), tr("New purchase"))
        self.tabs.addTab(self._build_list_tab(), tr("Purchases"))
        self.tabs.addTab(self._build_pay_tab(), tr("Pay supplier"))
        outer = QVBoxLayout(self)
        outer.addWidget(label(tr("Purchases"), "title"))
        outer.addWidget(self.lbl_status)
        outer.addWidget(self.tabs, 1)
        self.refresh()

    # ------------------------------------------------------------ layout: new purchase

    def _build_new_tab(self) -> QWidget:
        inner = QWidget()
        col = QVBoxLayout(inner)

        head = QGroupBox(tr("Supplier invoice"))
        row = QHBoxLayout(head)
        left, right = form(), form()
        self.cmb_supplier = QComboBox()
        self.txt_invoice = QLineEdit()
        self.txt_invoice.setPlaceholderText(tr("Invoice number on the supplier's bill"))
        self.txt_invoice_date = QLineEdit(today_text())
        self.txt_received_date = QLineEdit(today_text())
        left.addRow(tr("Supplier"), self.cmb_supplier)
        left.addRow(tr("Invoice number"), self.txt_invoice)
        left.addRow(tr("Invoice date (DD/MM/YYYY)"), self.txt_invoice_date)
        left.addRow(tr("Received on (DD/MM/YYYY)"), self.txt_received_date)
        self.txt_discount = QLineEdit("0")
        self.txt_freight = QLineEdit("0")
        self.txt_paid = QLineEdit("0")
        self.cmb_mode = QComboBox()
        for mode in svc.PAYMENT_MODES:
            self.cmb_mode.addItem(tr(MODE_TEXT[mode]), mode)
        self.txt_notes = QLineEdit()
        right.addRow(tr("Discount (₹)"), self.txt_discount)
        right.addRow(tr("Other charges (₹)"), self.txt_freight)
        right.addRow(tr("Paid now (₹)"), self.txt_paid)
        right.addRow(tr("Payment method"), self.cmb_mode)
        right.addRow(tr("Notes"), self.txt_notes)
        row.addLayout(left, 1)
        row.addLayout(right, 1)
        col.addWidget(head)
        self.lbl_duplicate = label("", "error")
        self.lbl_editing = label("", "subtitle")
        col.addWidget(self.lbl_duplicate)
        col.addWidget(self.lbl_editing)

        find = QHBoxLayout()
        self.txt_find = QLineEdit()
        self.txt_find.setPlaceholderText(tr("Search a product by name, code, barcode, size or colour"))
        self.btn_find = QPushButton(tr("Find product"))
        self.cmb_products = QComboBox()
        self.btn_grid = QPushButton(tr("Add sizes and colours…"))
        find.addWidget(self.txt_find, 2)
        find.addWidget(self.btn_find)
        find.addWidget(self.cmb_products, 2)
        find.addWidget(self.btn_grid)
        col.addLayout(find)

        self.tbl_lines = make_table([tr("Product"), tr("Size"), tr("Colour"), tr("Pieces"), tr("Cost per piece"),
                                     tr("GST %"), tr("GST"), tr("Line total")])
        col.addWidget(self.tbl_lines, 1)
        self.btn_remove = QPushButton(tr("Remove selected line"))
        col.addWidget(self.btn_remove)

        self.lbl_calc = label("", "error")
        col.addWidget(self.lbl_calc)
        totals = form()
        self.totals = {}
        self.totals["pieces"] = label("0")
        totals.addRow(tr("Pieces"), self.totals["pieces"])
        for key, name in (("subtotal", "Subtotal (before GST)"), ("tax", "GST"), ("discount", "Discount"),
                          ("freight", "Other charges"), ("round_off", "Round-off"), ("total", "Invoice total"),
                          ("paid", "Paid now"), ("due", "Still to pay")):
            self.totals[key] = label("")
            totals.addRow(tr(name), self.totals[key])
        col.addLayout(totals)

        buttons = QHBoxLayout()
        self.btn_review = QPushButton(tr("Review purchase…"))
        self.btn_review.setObjectName("primary")
        self.btn_clear = QPushButton(tr("Clear form"))
        buttons.addStretch(1)
        buttons.addWidget(self.btn_clear)
        buttons.addWidget(self.btn_review)
        col.addLayout(buttons)

        self.btn_find.clicked.connect(self.find_products)
        self.txt_find.returnPressed.connect(self.find_products)
        self.btn_grid.clicked.connect(self.add_grid)
        self.btn_remove.clicked.connect(self.remove_line)
        self.btn_review.clicked.connect(self.review_and_save)
        self.btn_clear.clicked.connect(self.clear_form)
        for edit in (self.txt_discount, self.txt_freight, self.txt_paid):
            edit.textChanged.connect(self._recalc)
        self.cmb_supplier.currentIndexChanged.connect(self._check_duplicate)
        self.txt_invoice.editingFinished.connect(self._check_duplicate)
        self.txt_invoice_date.editingFinished.connect(self._check_duplicate)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    # ------------------------------------------------------------ layout: purchases list

    def _build_list_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        top = QHBoxLayout()
        self.cmb_status = QComboBox()
        self.cmb_status.addItem(tr("All"), None)
        for key, text in STATUS_TEXT.items():
            self.cmb_status.addItem(tr(text), key)
        self.cmb_filter_supplier = QComboBox()
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText(tr("Search by invoice number, voucher or supplier"))
        top.addWidget(self.cmb_status)
        top.addWidget(self.cmb_filter_supplier)
        top.addWidget(self.txt_filter, 1)
        col.addLayout(top)

        self.tbl_purchases = make_table([tr("Invoice date"), tr("Supplier"), tr("Invoice number"), tr("Voucher"),
                                         tr("Status"), tr("Pieces"), tr("Invoice total"), tr("Paid"), tr("Still to pay")])
        col.addWidget(self.tbl_purchases, 2)
        self.txt_detail = QPlainTextEdit()
        self.txt_detail.setReadOnly(True)
        col.addWidget(self.txt_detail, 1)

        row = QHBoxLayout()
        self.cmb_confirm_mode = QComboBox()
        for mode in svc.PAYMENT_MODES:
            self.cmb_confirm_mode.addItem(tr(MODE_TEXT[mode]), mode)
        self.btn_confirm_draft = QPushButton(tr("Confirm draft and receive stock"))
        self.btn_confirm_draft.setObjectName("primary")
        self.btn_edit_draft = QPushButton(tr("Edit draft"))
        self.btn_cancel_draft = QPushButton(tr("Cancel draft"))
        row.addWidget(label(tr("Method for the amount paid:"), None, wrap=False))
        row.addWidget(self.cmb_confirm_mode)
        row.addStretch(1)
        for button in (self.btn_edit_draft, self.btn_cancel_draft, self.btn_confirm_draft):
            row.addWidget(button)
        col.addLayout(row)
        col.addWidget(label(tr("A confirmed purchase cannot be deleted. To send goods back, use Purchase Returns."), "hint"))

        self.cmb_status.currentIndexChanged.connect(self.reload_purchases)
        self.cmb_filter_supplier.currentIndexChanged.connect(self.reload_purchases)
        self.txt_filter.textChanged.connect(self.reload_purchases)
        self.tbl_purchases.itemSelectionChanged.connect(self._purchase_selected)
        self.btn_confirm_draft.clicked.connect(self.confirm_selected_draft)
        self.btn_edit_draft.clicked.connect(self.edit_selected_draft)
        self.btn_cancel_draft.clicked.connect(self.cancel_selected_draft)
        return page

    # ------------------------------------------------------------ layout: pay supplier

    def _build_pay_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        self.cmb_pay_supplier = QComboBox()
        self.lbl_balance = label("", "subtitle")
        col.addWidget(self.cmb_pay_supplier)
        col.addWidget(self.lbl_balance)
        self.tbl_bills = make_table([tr("Invoice number"), tr("Voucher"), tr("Invoice date"), tr("Age (days)"),
                                     tr("Invoice total"), tr("Paid"), tr("Credit for returns"), tr("Still to pay")])
        col.addWidget(self.tbl_bills, 1)

        fields = form()
        self.txt_pay_amount = QLineEdit()
        self.cmb_pay_mode = QComboBox()
        for mode in svc.PAYMENT_MODES:
            self.cmb_pay_mode.addItem(tr(MODE_TEXT[mode]), mode)
        self.txt_pay_ref = QLineEdit()
        self.txt_pay_date = QLineEdit(today_text())
        self.txt_pay_notes = QLineEdit()
        self.chk_advance = QCheckBox(tr("Advance payment (not for one bill)"))
        fields.addRow(tr("Amount (₹)"), self.txt_pay_amount)
        fields.addRow(tr("Payment method"), self.cmb_pay_mode)
        fields.addRow(tr("Reference (cheque or UPI number)"), self.txt_pay_ref)
        fields.addRow(tr("Date (DD/MM/YYYY)"), self.txt_pay_date)
        fields.addRow(tr("Notes"), self.txt_pay_notes)
        col.addLayout(fields)
        col.addWidget(self.chk_advance)
        self.btn_pay = QPushButton(tr("Record payment"))
        self.btn_pay.setObjectName("primary")
        col.addWidget(self.btn_pay)
        self.lbl_pay_note = label("", "hint")
        col.addWidget(self.lbl_pay_note)

        self.cmb_pay_supplier.currentIndexChanged.connect(self.reload_bills)
        self.tbl_bills.itemSelectionChanged.connect(self._bill_selected)
        self.btn_pay.clicked.connect(self.record_payment)
        if not self.can_pay:
            for widget in (self.txt_pay_amount, self.cmb_pay_mode, self.txt_pay_ref, self.txt_pay_date,
                           self.txt_pay_notes, self.chk_advance, self.btn_pay):
                widget.setEnabled(False)
            self.lbl_pay_note.setText(tr("You are not allowed to record supplier payments."))
        return page

    # ------------------------------------------------------------ loading

    def refresh(self) -> None:
        """Called each time the page is opened: reload suppliers and the lists (the form is kept)."""
        ok, suppliers = self._run(lambda: supplier_admin.list_suppliers(self.conn, self.user_id),
                                  title=tr("Purchases"), quiet_to=self.lbl_status)
        suppliers = suppliers or []
        pick = [(tr("Choose a supplier…"), None)] + [(s["name"], s["id"]) for s in suppliers]
        fill_combo(self.cmb_supplier, pick)
        fill_combo(self.cmb_filter_supplier, [(tr("All suppliers"), None)] + [(s["name"], s["id"]) for s in suppliers])
        fill_combo(self.cmb_pay_supplier, pick)
        self.reload_purchases()
        self.reload_bills()
        self._check_duplicate()

    def reload_purchases(self, *_):
        ok, rows = self._run(
            lambda: svc.list_purchases(
                self.conn, self.user_id, status=self.cmb_status.currentData(),
                supplier_id=self.cmb_filter_supplier.currentData(), text=self.txt_filter.text()),
            title=tr("Purchases"), quiet_to=self.lbl_status)
        self._purchases = rows or []
        self.tbl_purchases.setRowCount(0)
        for r in self._purchases:
            add_row(self.tbl_purchases, [
                svc.format_ui_date(r["invoice_date"]), r["supplier_name"], r["invoice_no"], r["voucher_no"] or "",
                tr(STATUS_TEXT[r["status"]]), r["qty"], format_rupees(r["total_paise"]),
                format_rupees(r["paid_paise"]), format_rupees(r["outstanding_paise"]),
            ], right={5, 6, 7, 8})
        self._purchase_selected()

    def reload_bills(self, *_):
        supplier_id = self.cmb_pay_supplier.currentData()
        self._bills = []
        self.tbl_bills.setRowCount(0)
        self.lbl_balance.setText("")
        if supplier_id is None:
            return
        ok, bills = self._run(
            lambda: supplier_ledger.supplier_outstanding_bills(self.conn, supplier_id, user_id=self.user_id),
            title=tr("Pay supplier"), quiet_to=self.lbl_status)
        self._bills = bills or []
        for b in self._bills:
            add_row(self.tbl_bills, [
                b["invoice_no"], b["voucher_no"] or "", svc.format_ui_date(b["invoice_date"]), b["age_days"],
                format_rupees(b["total_paise"]), format_rupees(b["paid_paise"]),
                format_rupees(b["returns_credit_paise"]), format_rupees(b["outstanding_paise"]),
            ], right={3, 4, 5, 6, 7})
        ok, balance = self._run(
            lambda: supplier_ledger.supplier_balance(self.conn, supplier_id, user_id=self.user_id),
            title=tr("Pay supplier"), quiet_to=self.lbl_status)
        if ok:
            owed = balance["balance_paise"]
            if owed > 0:
                self.lbl_balance.setText(tr("We owe this supplier: {amount}", amount=format_rupees(owed)))
            elif owed < 0:
                self.lbl_balance.setText(tr("This supplier owes us: {amount}", amount=format_rupees(-owed)))
            else:
                self.lbl_balance.setText(tr("Nothing is owed to this supplier."))

    # ------------------------------------------------------------ new purchase: products and lines

    def find_products(self):
        text = self.txt_find.text().strip()
        if not text:
            self._error(tr("Find product"), tr("Type a product name, code, barcode, size or colour first."))
            return
        ok, found = self._run(lambda: svc.find_products(self.conn, self.user_id, text), title=tr("Find product"))
        if not ok:
            return
        fill_combo(self.cmb_products,
                   [(p["name"] + (f" ({p['brand']})" if p["brand"] else ""), p["id"]) for p in found], keep=False)
        if not found:
            self._info(tr("Find product"), tr("No product found. Add new products in Products first."))

    def _open_grid(self, matrix):
        """Show the grid window. Returns the lines, or None if cancelled. (Tests replace this.)"""
        dialog = GridDialog(matrix, self)
        return dialog.lines if dialog.exec() else None

    def add_grid(self):
        product_id = self.cmb_products.currentData()
        if product_id is None:
            self._error(tr("Add sizes and colours"), tr("Find and choose a product first."))
            return
        ok, matrix = self._run(lambda: svc.product_matrix(self.conn, self.user_id, product_id),
                               title=tr("Add sizes and colours"))
        if not ok:
            return
        lines = self._open_grid(matrix)
        if lines:
            self._lines.extend(lines)
            self._recalc()

    def remove_line(self):
        row = self.tbl_lines.currentRow()
        if 0 <= row < len(self._lines):
            del self._lines[row]
            self._recalc()
        else:
            self._error(tr("Remove line"), tr("Select a line in the table first."))

    # ------------------------------------------------------------ new purchase: reading the form

    def _money(self, edit, name) -> int:
        text = edit.text().strip()
        if not text:
            return 0
        try:
            return parse_rupees(text)
        except ValueError as exc:
            raise ValueError(f"{name}: {tr(str(exc))}") from None

    def _amounts(self) -> dict:
        return {
            "discount_paise": self._money(self.txt_discount, tr("Discount")),
            "freight_paise": self._money(self.txt_freight, tr("Other charges")),
            "paid_paise": self._money(self.txt_paid, tr("Paid now")),
        }

    def _header(self) -> dict:
        """Everything typed above the lines, checked. Raises ValueError with a short message."""
        supplier_id = self.cmb_supplier.currentData()
        if supplier_id is None:
            raise ValueError(tr("Please choose a supplier."))
        invoice_no = self.txt_invoice.text().strip()
        if not invoice_no:
            raise ValueError(tr("Supplier invoice number is required."))
        try:
            invoice_date = svc.parse_ui_date(self.txt_invoice_date.text(), "Invoice date")
            received = svc.parse_ui_date(self.txt_received_date.text(), "Received date")
        except ValueError as exc:
            raise ValueError(tr(str(exc))) from None
        return {
            "supplier_id": supplier_id, "invoice_no": invoice_no, "invoice_date": invoice_date,
            "received_date": received, "notes": self.txt_notes.text().strip(), **self._amounts(),
        }

    def _recalc(self, *_):
        """Work out the invoice from the lines and show it. Nothing is saved."""
        self._summary = None
        try:
            amounts = self._amounts()
        except ValueError as exc:
            self.lbl_calc.setText(str(exc))
            self._show_lines(None)
            return
        if not self._lines:
            self.lbl_calc.setText("")
            self._show_lines(None)
            return
        ok, summary = self._run(
            lambda: svc.review_purchase(self.conn, self.user_id, self._lines, **amounts),
            title=tr("Purchase"), quiet_to=self.lbl_calc)
        self._summary = summary if ok else None
        self._show_lines(self._summary)

    def _show_lines(self, summary):
        self.tbl_lines.setRowCount(0)
        for index, line in enumerate(self._lines):
            calc = summary["lines"][index] if summary else None
            add_row(self.tbl_lines, [
                line["product"], line["size"], line["colour"], line["qty"], format_rupees(line["unit_cost_paise"]),
                rate_text(line["tax_rate_percent"]),
                format_rupees(calc["tax_paise"]) if calc else "", format_rupees(calc["line_total_paise"]) if calc else "",
            ], right={3, 4, 5, 6, 7})
        self.totals["pieces"].setText(str(summary["total_qty"]) if summary else str(sum(l["qty"] for l in self._lines)))
        keys = ("subtotal", "tax", "discount", "freight", "round_off", "total", "paid", "due")
        for key, (_, value) in zip(keys, totals_rows(summary) if summary else [("", "")] * len(keys)):
            self.totals[key].setText(value)

    def _check_duplicate(self, *_):
        """Warn (never block) when this supplier invoice number was already entered."""
        self.lbl_duplicate.setText("")
        supplier_id, number = self.cmb_supplier.currentData(), self.txt_invoice.text().strip()
        if supplier_id is None or not number:
            return
        try:
            found = svc.duplicate_invoices(self.conn, self.user_id, supplier_id, number,
                                           svc.parse_ui_date(self.txt_invoice_date.text(), "Invoice date"))
        except Exception:  # noqa: BLE001 - this is only a hint; the real check runs again when saving
            return
        if found:
            self.lbl_duplicate.setText(self._duplicate_text(found))

    @staticmethod
    def _duplicate_text(found) -> str:
        details = ", ".join(f"{f['voucher_no'] or tr('no voucher')} ({tr(STATUS_TEXT[f['status']])})" for f in found)
        return tr("This invoice number was already entered for this supplier: {details}. "
                  "Continue only if it is not the same bill again.", details=details)

    def clear_form(self):
        self._lines, self._summary, self._editing_id = [], None, None
        self.cmb_supplier.setCurrentIndex(0)
        for edit, text in ((self.txt_invoice, ""), (self.txt_invoice_date, today_text()),
                           (self.txt_received_date, today_text()), (self.txt_discount, "0"), (self.txt_freight, "0"),
                           (self.txt_paid, "0"), (self.txt_notes, "")):
            edit.setText(text)
        self.lbl_editing.setText("")
        self.lbl_duplicate.setText("")
        self._recalc()

    # ------------------------------------------------------------ new purchase: review, then save or confirm

    def _open_review(self, summary, header_text):
        """Show the review window. Returns 'draft', 'confirm' or None. (Tests replace this.)"""
        dialog = ReviewDialog(summary, header_text, self)
        return dialog.choice if dialog.exec() else None

    def review_and_save(self):
        if self._busy:
            return
        try:
            header = self._header()
        except ValueError as exc:
            self._error(tr("Review purchase"), str(exc))
            return
        if not self._lines:
            self._error(tr("Review purchase"), tr("Add at least one product with sizes and colours."))
            return
        ok, summary = self._run(
            lambda: svc.review_purchase(self.conn, self.user_id, self._lines, discount_paise=header["discount_paise"],
                                        freight_paise=header["freight_paise"], paid_paise=header["paid_paise"]),
            title=tr("Review purchase"))
        if not ok:
            return
        ok, found = self._run(
            lambda: svc.duplicate_invoices(self.conn, self.user_id, header["supplier_id"], header["invoice_no"],
                                           header["invoice_date"]), title=tr("Review purchase"))
        if not ok:
            return
        if found and not self._ask(tr("Same invoice number"), self._duplicate_text(found) + "\n\n" + tr("Continue?")):
            return
        head_text = tr("Supplier: {supplier}\nInvoice {invoice} dated {date}", supplier=self.cmb_supplier.currentText(),
                       invoice=header["invoice_no"], date=svc.format_ui_date(header["invoice_date"]))
        choice = self._open_review(summary, head_text)
        if choice in ("draft", "confirm"):
            self._finish(choice, header)

    def _finish(self, choice, header):
        """Save as draft, or save and confirm. Success is reported only after the core call returned."""
        if self._busy:
            return
        self._busy = True
        self.btn_review.setEnabled(False)
        done = None
        try:
            args = dict(
                received_date=header["received_date"], discount_paise=header["discount_paise"],
                freight_paise=header["freight_paise"], paid_paise=header["paid_paise"], notes=header["notes"],
                replaces_draft_id=self._editing_id,
            )
            if choice == "draft":
                ok, _ = self._run(
                    lambda: svc.save_draft(self.conn, self.user_id, header["supplier_id"], header["invoice_no"],
                                           header["invoice_date"], self._lines, **args),
                    title=tr("Save as draft"))
                if ok:
                    self._info(tr("Draft saved"), tr("The draft is saved. Stock has NOT changed yet. "
                                                    "Confirm it later from the Purchases list."))
            else:
                ok, done = self._run(
                    lambda: svc.receive_purchase(self.conn, self.user_id, header["supplier_id"], header["invoice_no"],
                                                 header["invoice_date"], self._lines,
                                                 payment_mode=self.cmb_mode.currentData(), **args),
                    title=tr("Confirm purchase"))
                if ok:
                    self._info(tr("Purchase confirmed"),
                               tr("Purchase confirmed. Voucher {voucher}. Stock has been updated.", voucher=done["voucher_no"]))
        finally:
            self._busy = False
            self.btn_review.setEnabled(True)
        if not ok:
            return  # nothing was saved: the form stays as it is so the user can fix it
        self.clear_form()
        self.reload_purchases()
        self.reload_bills()
        if done is not None:
            self._offer_labels(done["label_items"])

    def _offer_labels(self, items):
        """After stock is received, offer barcode labels. A print problem never undoes the purchase."""
        if not self.can_labels or not items:
            return
        pieces = sum(i["copies"] for i in items)
        if not self._ask(tr("Barcode labels"), tr("Print barcode labels for the {qty} pieces just received?", qty=pieces)):
            return
        try:
            result = print_service.print_labels(self.conn, self.session, items, parent=self)
        except Exception:  # noqa: BLE001
            log.exception("Label printing failed")
            self._error(tr("Barcode labels"), tr("The labels could not be printed. The purchase is saved."))
            return
        (self._info if result.ok else self._error)(tr("Barcode labels"), result.message)

    # ------------------------------------------------------------ purchases list: open, confirm, edit, cancel

    def _selected_purchase(self):
        row = self.tbl_purchases.currentRow()
        return self._purchases[row] if 0 <= row < len(self._purchases) else None

    def _purchase_selected(self, *_):
        chosen = self._selected_purchase()
        is_draft = chosen is not None and chosen["status"] == "draft"
        for button in (self.btn_confirm_draft, self.btn_edit_draft, self.btn_cancel_draft):
            button.setEnabled(is_draft)
        if chosen is None:
            self.txt_detail.setPlainText("")
            return
        ok, detail = self._run(lambda: svc.purchase_detail(self.conn, self.user_id, chosen["id"]),
                               title=tr("Purchases"), quiet_to=self.lbl_status)
        self.txt_detail.setPlainText(self._detail_text(detail) if ok else "")

    @staticmethod
    def _detail_text(detail) -> str:
        p = detail["purchase"]
        rows = [tr("{supplier}, invoice {invoice} dated {date}", supplier=detail["supplier_name"],
                   invoice=p["invoice_no"], date=svc.format_ui_date(p["invoice_date"]))]
        for l in detail["lines"]:
            rows.append(tr("{product} {size} / {colour}: {qty} x {cost}, GST {rate}%", product=l["product"],
                           size=l["size"], colour=l["colour"], qty=l["qty"], cost=format_rupees(l["unit_cost_paise"]),
                           rate=rate_text(l["tax_rate_percent"])))
        rows.append(tr("Invoice total {total}. Paid {paid}. Still to pay {due}.", total=format_rupees(p["total_paise"]),
                       paid=format_rupees(p["paid_paise"]), due=format_rupees(p["due_paise"])))
        return "\n".join(rows)

    def confirm_selected_draft(self):
        chosen = self._selected_purchase()
        if chosen is None or chosen["status"] != "draft" or self._busy:
            return
        if not self._ask(tr("Confirm purchase"), tr(
                "Confirm the draft from {supplier}, invoice {invoice}? Stock will go up by {qty} pieces.",
                supplier=chosen["supplier_name"], invoice=chosen["invoice_no"], qty=chosen["qty"])):
            return
        self._busy = True
        try:
            ok, done = self._run(
                lambda: svc.confirm_purchase(self.conn, self.user_id, chosen["id"],
                                             payment_mode=self.cmb_confirm_mode.currentData()),
                title=tr("Confirm purchase"))
            if ok:
                self._info(tr("Purchase confirmed"),
                           tr("Purchase confirmed. Voucher {voucher}. Stock has been updated.", voucher=done["voucher_no"]))
        finally:
            self._busy = False
        self.reload_purchases()  # a refused confirm (for example already confirmed) shows the real status again
        if ok:
            self.reload_bills()
            self._offer_labels(done["label_items"])

    def cancel_selected_draft(self):
        chosen = self._selected_purchase()
        if chosen is None or chosen["status"] != "draft":
            return
        if not self._ask(tr("Cancel draft"), tr("Cancel the draft for invoice {invoice}? Stock is not changed.",
                                                invoice=chosen["invoice_no"])):
            return
        ok, _ = self._run(lambda: svc.cancel_draft(self.conn, self.user_id, chosen["id"]), title=tr("Cancel draft"))
        if ok:
            self._info(tr("Draft cancelled"), tr("The draft was cancelled. Stock was not changed."))
        self.reload_purchases()

    def edit_selected_draft(self):
        """Copy a draft into the form. Saving it later replaces the old draft in one step."""
        chosen = self._selected_purchase()
        if chosen is None or chosen["status"] != "draft":
            return
        ok, detail = self._run(lambda: svc.purchase_detail(self.conn, self.user_id, chosen["id"]),
                               title=tr("Edit draft"))
        if not ok:
            return
        index = self.cmb_supplier.findData(detail["purchase"]["supplier_id"])
        if index < 0:
            self._error(tr("Edit draft"), tr("This supplier is disabled. Enable the supplier first."))
            return
        p = detail["purchase"]
        self.cmb_supplier.setCurrentIndex(index)
        self.txt_invoice.setText(p["invoice_no"])
        self.txt_invoice_date.setText(svc.format_ui_date(p["invoice_date"]))
        self.txt_received_date.setText(svc.format_ui_date(p["received_date"]) if p["received_date"] else today_text())
        self.txt_discount.setText(format_rupees(p["discount_paise"]))
        self.txt_freight.setText(format_rupees(p["freight_paise"]))
        self.txt_paid.setText(format_rupees(p["paid_paise"]))
        self.txt_notes.setText(p["notes"] or "")
        self._lines = [
            {"variant_id": l["variant_id"], "qty": l["qty"], "unit_cost_paise": l["unit_cost_paise"],
             "tax_rate_percent": l["tax_rate_percent"], "product": l["product"], "size": l["size"],
             "colour": l["colour"]}
            for l in detail["lines"]
        ]
        self._editing_id = p["id"]
        self.lbl_editing.setText(tr("You are editing the draft for invoice {invoice}. Saving it replaces that draft.",
                                    invoice=p["invoice_no"]))
        self._recalc()
        self.tabs.setCurrentIndex(0)

    # ------------------------------------------------------------ pay supplier

    def _selected_bill(self):
        row = self.tbl_bills.currentRow()
        return self._bills[row] if 0 <= row < len(self._bills) else None

    def _bill_selected(self, *_):
        bill = self._selected_bill()
        if bill is not None and not self.chk_advance.isChecked():
            self.txt_pay_amount.setText(format_rupees(bill["outstanding_paise"]))

    def record_payment(self):
        if self._busy:
            return
        supplier_id = self.cmb_pay_supplier.currentData()
        if supplier_id is None:
            self._error(tr("Pay supplier"), tr("Please choose a supplier."))
            return
        advance = self.chk_advance.isChecked()
        bill = None if advance else self._selected_bill()
        if bill is None and not advance:
            self._error(tr("Pay supplier"), tr("Select the bill you are paying, or tick 'Advance payment'."))
            return
        try:
            amount = parse_rupees(self.txt_pay_amount.text(), allow_zero=False)
            when = svc.parse_ui_date(self.txt_pay_date.text(), "Date")
        except ValueError as exc:
            self._error(tr("Pay supplier"), tr(str(exc)))
            return
        mode = self.cmb_pay_mode.currentData()
        name = self.cmb_pay_supplier.currentText()
        if not self._ask(tr("Pay supplier"), tr("Record a payment of {amount} to {supplier}?",
                                               amount=format_rupees(amount), supplier=name)):
            return
        common = dict(reference=self.txt_pay_ref.text(), notes=self.txt_pay_notes.text(), payment_date=when,
                      idempotency_key=self._pay_key)
        self._busy = True
        try:
            if bill is None:
                ok, _ = self._run(lambda: supplier_ledger.pay_supplier(
                    self.conn, self.user_id, supplier_id, amount, mode, **common), title=tr("Pay supplier"))
            else:
                ok, _ = self._run(lambda: svc.pay_supplier_bill(
                    self.conn, self.user_id, bill["purchase_id"], amount, mode, **common), title=tr("Pay supplier"))
            if ok:
                self._info(tr("Payment recorded"), tr("The payment of {amount} to {supplier} was recorded.",
                                                     amount=format_rupees(amount), supplier=name))
        finally:
            self._busy = False
        if ok:
            self._pay_key = uuid.uuid4().hex  # the next payment is a new one
            for edit in (self.txt_pay_amount, self.txt_pay_ref, self.txt_pay_notes):
                edit.setText("")
            self.chk_advance.setChecked(False)
            self.reload_bills()
            self.reload_purchases()

            