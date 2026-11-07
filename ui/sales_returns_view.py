# ui/sales_returns_view.py
"""Sales Returns and Exchanges screen (SRS section 12, RET-01 to RET-08).

The screen only shows things and hands what was typed to the tested core code. All money, stock and
number rules live in core.returns (one transaction per return) and the permission and owner-PIN rules
in core.guarded.return_items, so they hold even if this screen is wrong.

Two tabs:
  * Return or exchange: find the bill by its number (or part of it), the customer's phone and/or the date,
    choose the pieces, the reason and the outcome (refund, store credit or exchange). A walk-in bill that
    ends in store credit needs a saved customer to give the credit to: the screen asks for one. For an exchange the replacement items are chosen here and the
    screen shows what the customer pays or gets back BEFORE anything is saved.
  * Returned items to check: returned pieces are NOT sellable until an authorised user decides they
    are fit for resale or damaged (RET-05).

Rules kept here:
  * "Saved" is shown only after the core call really returned; a failure shows a short message and
    nothing is changed.
  * A double click or a retry cannot return the same pieces twice: every attempt carries an
    idempotency key and the button is blocked while an action runs.
  * Technical detail of a fault goes to the log only.
"""

import logging
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGroupBox, QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from core import bill_preview as bp
from core import guarded, returns as rt, stock_ops
from core.auth import AuthError
from core.customers import search_customers
from core.money import format_rupees, parse_rupees
from core.permissions import has_permission
from core.products import search_products
from core.purchase_service import format_ui_date, parse_ui_date, safe_message
from core.sales import enabled_payment_modes, find_sale_by_invoice_no, find_sales, get_sale
from ui.billing_counter_view import VariantPickerDialog
from ui.i18n import N_, tr
from ui.screen_helpers import MODE_TEXT, ScreenBase, add_row, make_table
from ui.widgets import form, label

log = logging.getLogger("clothshop")

# The text saved on the credit note is the English one; the screen shows it translated.
REASON_TEXT = {
    "wrong_size": N_("Wrong size"),
    "wrong_colour": N_("Wrong colour"),
    "defect": N_("Defect"),
    "customer_preference": N_("Customer preference"),
    "other": N_("Other"),
}
OUTCOME_TEXT = {
    "refund": N_("Refund the money"),
    "store_credit": N_("Store credit"),
    "exchange": N_("Exchange for other items"),
}
SURPLUS_TEXT = {"refund": N_("Give the difference back"), "store_credit": N_("Add the difference to store credit")}


def _plain(paise: int) -> str:
    """Whole rupees and paise as typed in an amount box, e.g. 30000 -> '300.00'."""
    return f"{paise // 100}.{paise % 100:02d}"


class ReplacementDialog(QDialog):
    """Search a product, then pick its size and colour. Result: .variant_id and .qty."""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.variant_id, self.qty = None, 1
        self.setWindowTitle(tr("Choose the replacement item"))
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(tr("Search by name, code or barcode"))
        self.results = QListWidget()
        self.message = label("", "error")
        col = QVBoxLayout(self)
        col.addWidget(self.txt_search)
        col.addWidget(self.results, 1)
        col.addWidget(self.message)
        self.txt_search.textChanged.connect(self._fill)
        self.txt_search.returnPressed.connect(self._fill)
        self.results.itemActivated.connect(self._pick)
        self.resize(520, 380)

    def _fill(self, *_):
        self.results.clear()
        text = self.txt_search.text().strip()
        if not text:
            return
        found = bp.find_by_code(self.conn, text)  # a scanned barcode or SKU goes straight to its variant
        if found:
            item = QListWidgetItem(found["label"])
            item.setData(Qt.UserRole, ("variant", found["variant_id"]))
            self.results.addItem(item)
        for p in search_products(self.conn, text, limit=20):
            item = QListWidgetItem(p["name"] + (f"  ({p['brand']})" if p["brand"] else ""))
            item.setData(Qt.UserRole, ("product", p["id"], p["name"]))
            self.results.addItem(item)

    def _pick(self, item):
        kind = item.data(Qt.UserRole)
        if kind[0] == "variant":
            self.variant_id, self.qty = kind[1], 1
            self.accept()
            return
        picker = VariantPickerDialog(self.conn, kind[1], kind[2], self)
        if picker.exec() == QDialog.Accepted:
            self.variant_id, self.qty = picker.variant_id, picker.qty
            self.accept()


class ReturnCustomerDialog(QDialog):
    """Choose the saved customer who gets the store credit of a walk-in bill. Result: .customer_id, .customer_name.

    Lists active saved customers only (newest first, or those matching what was typed). Needs the
    view-customers permission; a fault shows a short message without technical detail.
    """

    def __init__(self, conn, user_id, parent=None):
        super().__init__(parent)
        self.conn, self.user_id = conn, user_id
        self.customer_id, self.customer_name = None, None
        self.setWindowTitle(tr("Choose the customer"))
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(tr("Search by name or phone number"))
        self.results = QListWidget()
        self.message = label("", "error")
        col = QVBoxLayout(self)
        col.addWidget(self.txt_search)
        col.addWidget(self.results, 1)
        col.addWidget(self.message)
        self.txt_search.textChanged.connect(self._fill)
        self.results.itemActivated.connect(self._pick)
        self.results.itemDoubleClicked.connect(self._pick)
        self.resize(480, 360)
        self._fill()

    def _fill(self, *_):
        self.results.clear()
        self.message.setText("")
        try:
            rows = search_customers(self.conn, self.txt_search.text(), user_id=self.user_id, limit=50)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a short message
            text, expected = safe_message(exc)
            if not expected:
                log.exception("Customer search failed")
            self.message.setText(tr(text) if expected else tr("The customers could not be loaded."))
            return
        for r in rows:
            name = r["name"] or r["phone"]
            item = QListWidgetItem(f"{name}   {r['phone']}" if r["name"] else r["phone"])
            item.setData(Qt.UserRole, (r["id"], name))
            self.results.addItem(item)
        if not rows:
            self.message.setText(tr("No customer found. Add the customer in the Customers menu first."))

    def _pick(self, item):
        self.customer_id, self.customer_name = item.data(Qt.UserRole)
        self.accept()


class SalesReturnsView(ScreenBase, QWidget):
    """Return or exchange items from a saved bill, and check the returned items afterwards."""

    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self._sale_id = None
        self._bill = None         # get_sale() of the bill that is open
        self._lines = []          # returnable_lines() of that bill
        self._boxes = []          # one spin box per line: pieces to return
        self._cart = []           # replacement items for an exchange
        self._pending = []        # rows waiting for the resale check
        self._busy = False
        self._found = []          # bills listed by the search
        self._credit_customer = None  # (id, name) chosen to get the store credit of a walk-in bill
        self._difference = None   # what the customer pays (+) or gets back (-) in an exchange, once known
        self._key = uuid.uuid4().hex  # one key per return attempt, renewed after a successful one

        self.lbl_status = label("", "error")
        self.tabs = QTabWidget()

        # ================= tab 1: return or exchange =================
        page = QWidget()
        col = QVBoxLayout(page)

        find = QHBoxLayout()
        self.txt_invoice = QLineEdit()
        self.txt_invoice.setPlaceholderText(tr("Bill number, for example INV/26-27/000123"))
        self.btn_find = QPushButton(tr("Find bill"))
        find.addWidget(self.txt_invoice, 1)
        find.addWidget(self.btn_find)
        col.addLayout(find)
        search = QHBoxLayout()
        self.txt_phone = QLineEdit()
        self.txt_phone.setPlaceholderText(tr("Customer phone (at least 4 digits)"))
        self.txt_from, self.txt_to = QLineEdit(), QLineEdit()
        self.txt_from.setPlaceholderText("DD/MM/YYYY")
        self.txt_to.setPlaceholderText("DD/MM/YYYY")
        self.btn_search = QPushButton(tr("Search bills"))
        search.addWidget(self.txt_phone, 2)
        search.addWidget(self.txt_from, 1)
        search.addWidget(self.txt_to, 1)
        search.addWidget(self.btn_search)
        col.addLayout(search)
        self.tbl_found = make_table([tr("Bill"), tr("Bill date"), tr("Bill total"), tr("Status")])
        self.tbl_found.setMaximumHeight(150)
        self.tbl_found.setVisible(False)
        self.btn_open_found = QPushButton(tr("Open selected bill"))
        self.btn_open_found.setVisible(False)
        col.addWidget(self.tbl_found)
        col.addWidget(self.btn_open_found)
        self.lbl_bill = label("", "hint")
        col.addWidget(self.lbl_bill)

        self.tbl_lines = make_table([
            tr("Product"), tr("Size"), tr("Colour"), tr("Sold"), tr("Already returned"), tr("Unit price"),
            tr("Return pieces"),
        ])
        col.addWidget(self.tbl_lines, 2)

        details = form()
        self.cmb_reason = QComboBox()
        for key, text in REASON_TEXT.items():
            self.cmb_reason.addItem(tr(text), key)
        self.txt_note = QLineEdit()
        self.txt_note.setPlaceholderText(tr("Note (required when the reason is Other)"))
        self.cmb_outcome = QComboBox()
        for key in rt.OUTCOMES:
            self.cmb_outcome.addItem(tr(OUTCOME_TEXT[key]), key)
        self.cmb_refund_mode = QComboBox()
        self.cmb_refund_mode.addItem(tr("Same as the original payment"), None)
        for mode in enabled_payment_modes(conn):  # only the methods the shop accepts now
            self.cmb_refund_mode.addItem(tr(MODE_TEXT[mode]), mode)
        details.addRow(tr("Reason"), self.cmb_reason)
        details.addRow(tr("Note"), self.txt_note)
        details.addRow(tr("What the customer wants"), self.cmb_outcome)
        details.addRow(tr("Refund method"), self.cmb_refund_mode)
        col.addLayout(details)
        self.row_credit = QWidget()
        credit_row = QHBoxLayout(self.row_credit)
        credit_row.setContentsMargins(0, 0, 0, 0)
        self.lbl_credit_customer = label("", "hint")
        self.btn_pick_customer = QPushButton(tr("Choose customer"))
        credit_row.addWidget(self.lbl_credit_customer, 1)
        credit_row.addWidget(self.btn_pick_customer)
        self.row_credit.setVisible(False)
        col.addWidget(self.row_credit)

        # ---- exchange part
        self.box_exchange = QGroupBox(tr("Replacement items"))
        ex = QVBoxLayout(self.box_exchange)
        self.tbl_replacement = make_table([tr("Item"), tr("Pieces"), tr("Price")])
        ex.addWidget(self.tbl_replacement)
        ex_buttons = QHBoxLayout()
        self.btn_add_replacement = QPushButton(tr("Add replacement item"))
        self.btn_remove_replacement = QPushButton(tr("Remove selected"))
        ex_buttons.addWidget(self.btn_add_replacement)
        ex_buttons.addWidget(self.btn_remove_replacement)
        ex_buttons.addStretch(1)
        ex.addLayout(ex_buttons)
        pay = QHBoxLayout()
        self.cmb_surplus = QComboBox()
        for key, text in SURPLUS_TEXT.items():
            self.cmb_surplus.addItem(tr(text), key)
        self.cmb_pay_mode = QComboBox()
        for mode in enabled_payment_modes(conn):  # only the methods the shop accepts now
            self.cmb_pay_mode.addItem(tr(MODE_TEXT[mode]), mode)
        self.txt_pay_amount = QLineEdit()
        self.txt_pay_amount.setPlaceholderText(tr("Amount the customer pays"))
        self.lbl_pay = label(tr("Customer pays"))
        pay.addWidget(self.cmb_surplus)
        pay.addWidget(self.lbl_pay)
        pay.addWidget(self.cmb_pay_mode)
        pay.addWidget(self.txt_pay_amount)
        ex.addLayout(pay)
        col.addWidget(self.box_exchange)

        self.lbl_preview = label("", "title")
        col.addWidget(self.lbl_preview)
        self.btn_process = QPushButton(tr("Save return"))
        self.btn_process.setObjectName("primary")
        col.addWidget(self.btn_process)

        hist = QGroupBox(tr("Earlier returns on this bill"))
        hist_col = QVBoxLayout(hist)
        self.tbl_history = make_table([
            tr("Credit note"), tr("Date"), tr("What happened"), tr("Total"), tr("Refunded"), tr("Store credit"),
        ])
        hist_col.addWidget(self.tbl_history)
        col.addWidget(hist, 1)
        self.tabs.addTab(page, tr("Return or exchange"))

        # ================= tab 2: returned items to check =================
        self.can_review = has_permission(conn, self.user_id, "adjust_stock")
        self.tbl_pending = make_table([tr("Product"), tr("Size"), tr("Colour"), tr("Waiting for check")])
        self.spin_review = QSpinBox()
        self.spin_review.setRange(1, 1)
        self.txt_review_note = QLineEdit()
        self.txt_review_note.setPlaceholderText(tr("Note (optional)"))
        self.btn_fit = QPushButton(tr("Fit for resale"))
        self.btn_damaged = QPushButton(tr("Damaged, cannot be sold"))
        if self.can_review:
            check = QWidget()
            cc = QVBoxLayout(check)
            cc.addWidget(label(tr("Returned pieces cannot be sold until you check them."), "hint"))
            cc.addWidget(self.tbl_pending, 1)
            row = QHBoxLayout()
            row.addWidget(label(tr("Pieces")))
            row.addWidget(self.spin_review)
            row.addWidget(self.txt_review_note, 1)
            row.addWidget(self.btn_fit)
            row.addWidget(self.btn_damaged)
            cc.addLayout(row)
            self.tabs.addTab(check, tr("Returned items to check"))

        outer = QVBoxLayout(self)
        outer.addWidget(label(tr("Sales Returns and Exchanges"), "title"))
        outer.addWidget(self.lbl_status)
        outer.addWidget(self.tabs, 1)

        self.btn_find.clicked.connect(self.find_bill)
        self.btn_search.clicked.connect(self.search_bills)
        self.txt_phone.returnPressed.connect(self.search_bills)
        self.btn_open_found.clicked.connect(self.open_found_bill)
        self.tbl_found.cellDoubleClicked.connect(self._found_double_clicked)
        self.btn_pick_customer.clicked.connect(self.choose_credit_customer)
        self.txt_invoice.returnPressed.connect(self.find_bill)
        self.cmb_outcome.currentIndexChanged.connect(self._outcome_changed)
        self.cmb_surplus.currentIndexChanged.connect(self._outcome_changed)
        self.btn_add_replacement.clicked.connect(self.choose_replacement)
        self.btn_remove_replacement.clicked.connect(self.remove_replacement)
        self.btn_process.clicked.connect(self.process)
        self.tbl_pending.currentCellChanged.connect(self._pending_selected)
        self.btn_fit.clicked.connect(self._review_fit)          # plain methods, not lambdas: a lambda that
        self.btn_damaged.clicked.connect(self._review_damaged)  # holds the screen can crash Qt when it closes
        self._outcome_changed()
        self.refresh()

    # ------------------------------------------------------------ small pop-ups (tests replace these)

    def _ask_pin(self):
        text, ok = QInputDialog.getText(self, tr("Owner approval"), tr("Owner PIN"), QLineEdit.Password)
        return text.strip() if ok else None

    def _ask_override_reason(self):
        text, ok = QInputDialog.getText(self, tr("Owner approval"), tr("Reason for allowing this return"))
        return text.strip() if ok else None

    # ------------------------------------------------------------ loading

    def refresh(self) -> None:
        """Called each time the page is opened."""
        if self._sale_id is not None:
            self._load_sale(self._sale_id)
        self.reload_pending()

    def find_bill(self) -> None:
        self._credit_customer = None  # a customer chosen for another bill never carries over
        number = self.txt_invoice.text().strip()
        if not number:
            self.lbl_status.setText(tr("Type the bill number first."))
            return
        ok, sale_id = self._run(lambda: find_sale_by_invoice_no(self.conn, number), title=tr("Sales Returns and Exchanges"),
                                quiet_to=self.lbl_status)
        if not ok:
            return
        if sale_id is None:
            self._clear_bill()
            self.lbl_status.setText(tr("No bill with that number was found."))
            return
        self._cart = []
        self._load_sale(sale_id)

    def _clear_bill(self):
        self._sale_id, self._bill, self._lines, self._boxes = None, None, [], []
        self._credit_customer = None
        self.lbl_bill.setText("")
        self.tbl_lines.setRowCount(0)
        self.tbl_history.setRowCount(0)
        self._update_preview()

    def _load_sale(self, sale_id) -> None:
        title = tr("Sales Returns and Exchanges")
        ok, bill = self._run(lambda: get_sale(self.conn, sale_id), title=title, quiet_to=self.lbl_status)
        if not ok:
            return
        sale = bill["sale"]
        self._sale_id, self._bill = sale_id, bill
        if sale["status"] != "completed":
            self._lines, self._boxes = [], []
            self.tbl_lines.setRowCount(0)
            self.lbl_bill.setText(f"{sale['invoice_no']}")
            self.lbl_status.setText(tr("A cancelled bill cannot be returned."))
            self._update_preview()
            return
        ok, lines = self._run(lambda: rt.returnable_lines(self.conn, sale_id), title=title, quiet_to=self.lbl_status)
        self._lines = lines or []
        who = tr("Saved customer") if sale["customer_id"] is not None else tr("Walk-in customer")
        text = tr("{no}   {date}   {who}   Total {total}", no=sale["invoice_no"], date=format_ui_date(sale["created_at"][:10]),
                  who=who, total=format_rupees(sale["total_paise"]))
        if bill["due_paise"] > 0:
            text += "   " + tr("Still unpaid: {amount}", amount=format_rupees(bill["due_paise"]))
        self.lbl_bill.setText(text)

        self.tbl_lines.setRowCount(0)
        self._boxes = []
        for line in self._lines:
            row = add_row(self.tbl_lines, [
                line["product_name"], line["size"] or "", line["colour"] or "", line["qty_sold"],
                line["qty_returned"], format_rupees(line["unit_price_paise"]), "",
            ], right={3, 4, 5})
            spin = QSpinBox()
            spin.setRange(0, line["qty_returnable"])
            spin.setEnabled(line["qty_returnable"] > 0)
            spin.valueChanged.connect(self._update_preview)
            self.tbl_lines.setCellWidget(row, 6, spin)
            self._boxes.append(spin)

        self.tbl_history.setRowCount(0)
        ok, history = self._run(lambda: rt.list_returns_for_sale(self.conn, sale_id), title=title, quiet_to=self.lbl_status)
        for h in history or []:
            add_row(self.tbl_history, [
                h["credit_note_no"], format_ui_date((h.get("created_at") or "")[:10]) if h.get("created_at") else "",
                tr(OUTCOME_TEXT.get(h["outcome"], h["outcome"])), format_rupees(h["total_paise"]),
                format_rupees(h["refund_paise"]), format_rupees(h["store_credit_paise"]),
            ], right={3, 4, 5})
        self._update_preview()

    # ------------------------------------------------------------ what was chosen

    def _items(self) -> list:
        return [{"sale_line_id": line["sale_line_id"], "qty": box.value()}
                for line, box in zip(self._lines, self._boxes) if box.value() > 0]

    def _outcome(self) -> str:
        return self.cmb_outcome.currentData()

    def _reason_text(self):
        """The reason saved on the credit note, or None (with a message shown) when it is incomplete."""
        key = self.cmb_reason.currentData()
        note = self.txt_note.text().strip()
        if key == "other" and not note:
            self.lbl_status.setText(tr("Write the reason in the note box."))
            return None
        text = REASON_TEXT[key]
        return f"{text}: {note}" if note else text

    def _outcome_changed(self, *_):
        exchange = self._outcome() == "exchange"
        self.box_exchange.setVisible(exchange)
        self._update_preview()

    # ------------------------------------------------------------ replacement items (exchange)

    def choose_replacement(self) -> None:
        dialog = ReplacementDialog(self.conn, self)
        if dialog.exec() == QDialog.Accepted and dialog.variant_id is not None:
            self.add_replacement(dialog.variant_id, dialog.qty)

    def add_replacement(self, variant_id, qty=1) -> bool:
        """Put a replacement item on the exchange. Blocked, with a reason, when it cannot be sold."""
        info = bp.variant_info(self.conn, variant_id)
        if info is None:
            self.lbl_status.setText(tr("That product was not found."))
            return False
        if not info["is_active"]:
            self.lbl_status.setText(tr("{item} is inactive and cannot be sold.", item=info["label"]))
            return False
        if info["price_paise"] <= 0:
            self.lbl_status.setText(tr("{item} has no selling price. Set the price first.", item=info["label"]))
            return False
        line = next((l for l in self._cart if l["variant_id"] == variant_id), None)
        wanted = (line["qty"] if line else 0) + qty
        if wanted > info["sellable"]:
            if info["sellable"] <= 0:
                self.lbl_status.setText(tr("{item} is out of stock.", item=info["label"]))
            else:
                self.lbl_status.setText(tr("{item}: only {n} in stock.", item=info["label"], n=info["sellable"]))
            return False
        if line:
            line["qty"] = wanted
        else:
            self._cart.append({"variant_id": variant_id, "qty": qty, "discount_paise": 0})
        self.lbl_status.setText("")
        self._paint_cart()
        self._update_preview()
        return True

    def remove_replacement(self) -> None:
        row = self.tbl_replacement.currentRow()
        if 0 <= row < len(self._cart):
            del self._cart[row]
            self._paint_cart()
            self._update_preview()

    def _paint_cart(self) -> None:
        self.tbl_replacement.setRowCount(0)
        for line in self._cart:
            info = bp.variant_info(self.conn, line["variant_id"])
            add_row(self.tbl_replacement, [info["label"], line["qty"], format_rupees(info["price_paise"])], right={1, 2})

    # ------------------------------------------------------------ the amounts shown before saving

    def _update_preview(self, *_) -> None:
        """Show what the customer gets or pays. Nothing is saved here."""
        exchange = self._outcome() == "exchange"
        items = self._items() if self._sale_id is not None else []
        show_pay, show_surplus, refund_possible = False, False, self._outcome() == "refund"
        text = ""
        self._difference = None
        if items:
            cart = self._cart if exchange else None
            ok, p = self._run(lambda: rt.preview_return(self.conn, self._sale_id, items, exchange_cart=cart),
                              title=tr("Sales Returns and Exchanges"), quiet_to=self.lbl_status)
            if ok:
                lines = [tr("Value of the returned pieces: {amount}", amount=format_rupees(p["returned_value_paise"]))]
                if p["due_adjusted_paise"]:
                    lines.append(tr("Reduces the unpaid balance of the bill by {amount}",
                                    amount=format_rupees(p["due_adjusted_paise"])))
                if exchange and p["difference_paise"] is not None:
                    lines.append(tr("Replacement items: {amount}", amount=format_rupees(p["replacement_total_paise"])))
                    diff = p["difference_paise"]
                    self._difference = diff
                    if diff > 0:
                        lines.append(tr("Customer pays: {amount}", amount=format_rupees(diff)))
                        show_pay = True
                        self.txt_pay_amount.setText(_plain(diff))
                    elif diff < 0:
                        lines.append(tr("Customer gets back: {amount}", amount=format_rupees(-diff)))
                        show_surplus = True
                        refund_possible = self.cmb_surplus.currentData() == "refund"
                    else:
                        lines.append(tr("Even exchange, nothing to pay."))
                elif exchange:
                    lines.append(tr("Add the replacement items."))
                else:
                    lines.append(tr("Customer gets back: {amount}", amount=format_rupees(p["credit_paise"]))
                                 if self._outcome() == "refund" else
                                 tr("Added to store credit: {amount}", amount=format_rupees(p["credit_paise"])))
                text = "\n".join(lines)
        self.lbl_preview.setText(text)
        self.lbl_pay.setVisible(show_pay)
        self.cmb_pay_mode.setVisible(show_pay)
        self.txt_pay_amount.setVisible(show_pay)
        self.cmb_surplus.setVisible(show_surplus)
        self.cmb_refund_mode.setEnabled(refund_possible)
        self.btn_process.setEnabled(bool(self._lines) and not self._busy)
        self._refresh_credit_row()

    # ------------------------------------------------------------ who gets the store credit

    def _credit_needed(self) -> bool:
        """True when store credit will be given on a walk-in bill, so a saved customer must be chosen."""
        if self._bill is None or self._bill["sale"]["customer_id"] is not None or not self._items():
            return False
        outcome = self._outcome()
        if outcome == "store_credit":
            return True
        return (outcome == "exchange" and self.cmb_surplus.currentData() == "store_credit"
                and self._difference is not None and self._difference < 0)

    def _refresh_credit_row(self) -> None:
        self.row_credit.setVisible(self._credit_needed())
        if self._credit_customer:
            self.lbl_credit_customer.setText(tr("Store credit goes to: {name}", name=self._credit_customer[1]))
            self.btn_pick_customer.setText(tr("Change customer"))
        else:
            self.lbl_credit_customer.setText(tr("This was a walk-in bill. Choose the customer who gets the store credit."))
            self.btn_pick_customer.setText(tr("Choose customer"))

    def set_credit_customer(self, customer_id, name) -> None:
        self._credit_customer = (customer_id, name)
        self._refresh_credit_row()

    def choose_credit_customer(self) -> None:
        dialog = ReturnCustomerDialog(self.conn, self.user_id, self)
        if dialog.exec() == QDialog.Accepted and dialog.customer_id is not None:
            self.set_credit_customer(dialog.customer_id, dialog.customer_name)

    # ------------------------------------------------------------ finding a bill by phone, date or part of its number

    def search_bills(self) -> None:
        self._found = []
        self.tbl_found.setRowCount(0)
        self.tbl_found.setVisible(False)
        self.btn_open_found.setVisible(False)

        def search():
            return find_sales(
                self.conn, user_id=self.user_id, text=self.txt_invoice.text(), phone=self.txt_phone.text(),
                date_from=parse_ui_date(self.txt_from.text(), "The first date") if self.txt_from.text().strip() else None,
                date_to=parse_ui_date(self.txt_to.text(), "The second date") if self.txt_to.text().strip() else None)

        ok, rows = self._run(search, title=tr("Sales Returns and Exchanges"), quiet_to=self.lbl_status)
        if not ok:
            return
        if not rows:
            self.lbl_status.setText(tr("No bills found. Check the number, phone or dates."))
            return
        self._found = rows
        for r in rows:
            add_row(self.tbl_found, [r["invoice_no"], format_ui_date(r["created_at"][:10]), format_rupees(r["total_paise"]),
                                     tr("Cancelled") if r["status"] != "completed" else ""], right={2})
        self.tbl_found.setVisible(True)
        self.btn_open_found.setVisible(True)
        self.lbl_status.setText(tr("{n} bill(s) found. Double-click one to open it.", n=len(rows)))

    def _open_found(self, row) -> None:
        bill = self._found[row]
        self.txt_invoice.setText(bill["invoice_no"])
        self._credit_customer = None
        self._cart = []
        self.lbl_status.setText("")
        self._load_sale(bill["id"])

    def _found_double_clicked(self, row, _col) -> None:
        if 0 <= row < len(self._found):
            self._open_found(row)

    def open_found_bill(self) -> None:
        selection = self.tbl_found.selectionModel()
        rows = selection.selectedRows() if selection is not None else []
        row = rows[0].row() if rows else self.tbl_found.currentRow()
        if not 0 <= row < len(self._found):
            self.lbl_status.setText(tr("Select a bill in the list first."))
            return
        self._open_found(row)

    # ------------------------------------------------------------ the action

    def process(self) -> None:
        """Save the return or exchange. Says 'saved' only after the core call really returned."""
        if self._busy:
            return
        title = tr("Sales Returns and Exchanges")
        if self._sale_id is None:
            self._error(title, tr("Find the bill first."))
            return
        reason = self._reason_text()
        if reason is None:
            return
        items = self._items()
        outcome = self._outcome()
        exchange = outcome == "exchange"
        for item in items:
            item["reason"] = reason

        needs_customer = self._credit_needed()
        credit_customer = self._credit_customer if needs_customer else None
        if needs_customer and credit_customer is None:
            self._error(title, tr("Choose the customer who gets the store credit."))
            return

        payments = []
        if exchange and self.txt_pay_amount.isVisibleTo(self) and self.txt_pay_amount.text().strip():
            try:
                payments = [{"mode": self.cmb_pay_mode.currentData(),
                             "amount_paise": parse_rupees(self.txt_pay_amount.text(), allow_zero=False)}]
            except ValueError as exc:
                self._error(title, str(exc))
                return
        surplus = self.cmb_surplus.currentData() if exchange else "refund"
        refund_mode = self.cmb_refund_mode.currentData() if self.cmb_refund_mode.isEnabled() else None
        kwargs = {
            "outcome": outcome, "reason": reason, "refund_mode": refund_mode, "surplus_outcome": surplus,
            "exchange_cart": list(self._cart) if exchange else None, "payments": payments,
            "customer_id": credit_customer[0] if credit_customer else None,
        }

        if items:
            question = tr("Save this {kind}? Stock and money will change and this cannot be undone.",
                          kind=tr("exchange") if exchange else tr("return"))
            if credit_customer:
                question += "\n\n" + tr("Any store credit is saved for {name}.", name=credit_customer[1])
            if not self._ask(title, question):
                return

        self._busy = True
        self.btn_process.setEnabled(False)
        try:
            return_id = self._attempt(items, kwargs, title)
        finally:
            self._busy = False
        if return_id is None:
            self._update_preview()
            return
        self._after_saved(return_id, title, credit_customer[1] if credit_customer else None)

    def _attempt(self, items, kwargs, title):
        """Call the guarded core action; ask for the owner's approval when the shop's policy blocks it.
        Returns the credit note id or None (the reason has been shown)."""
        extra = {}
        for _ in range(3):
            try:
                return guarded.return_items(self.conn, self._sale_id, items, user_id=self.user_id,
                                            idempotency_key=self._key, **kwargs, **extra)
            except AuthError as exc:
                self._error(title, str(exc))
                return None
            except Exception as exc:  # noqa: BLE001 - every failure becomes a short message
                text, expected = safe_message(exc)
                if not expected:
                    log.exception("Sales return failed")
                if expected and "Only the owner can override" in text and "approver_pin" not in extra:
                    if not self._ask(title, tr(text)):
                        return None
                    pin = self._ask_pin()
                    reason = self._ask_override_reason() if pin else None
                    if not pin or not reason:
                        return None
                    extra = {"approver_pin": pin, "override_reason": reason}
                    continue
                if expected and "reason for overriding" in text and "override_reason" not in extra:
                    if not self._ask(title, tr("This return is outside the shop's return policy. Allow it anyway?")):
                        return None
                    reason = self._ask_override_reason()
                    if not reason:
                        return None
                    extra["override_reason"] = reason
                    continue
                self._error(title, tr(text))
                return None
        return None

    def _after_saved(self, return_id, title, credit_name=None) -> None:
        self._credit_customer = None  # the choice never carries over to the next return
        ok, data =  self._run(lambda: rt.get_return(self.conn, return_id), title=title, quiet_to=self.lbl_status)
        self._key = uuid.uuid4().hex
        self.txt_note.clear()
        self.cmb_reason.setCurrentIndex(0)
        self._cart = []
        self._paint_cart()
        message = [tr("Credit note {no} saved.", no=data["return"]["credit_note_no"] if ok else "")]
        if ok:
            r = data["return"]
            message.append(tr("Value of the returned pieces: {amount}", amount=format_rupees(r["total_paise"])))
            if r["due_adjusted_paise"]:
                message.append(tr("Reduced the unpaid balance by {amount}", amount=format_rupees(r["due_adjusted_paise"])))
            if r["refund_paise"]:
                message.append(tr("Refunded: {amount}", amount=format_rupees(r["refund_paise"])))
            if r["store_credit_paise"]:
                message.append(tr("Added to store credit: {amount}", amount=format_rupees(r["store_credit_paise"])))
                if credit_name:
                    message.append(tr("Store credit saved for {name}.", name=credit_name))
            if data["exchange_invoice_no"]:
                message.append(tr("Replacement bill: {no}", no=data["exchange_invoice_no"]))
            message.append(tr("The returned pieces are waiting for a check before they can be sold again."))
        self._info(title, "\n".join(message))
        self._load_sale(self._sale_id)
        self.reload_pending()

    # ------------------------------------------------------------ returned items to check

    def reload_pending(self) -> None:
        if not self.can_review:
            return
        keep = self.tbl_pending.currentRow()
        ok, rows = self._run(lambda: stock_ops.list_returned_pending(self.conn, user_id=self.user_id),
                             title=tr("Returned items to check"), quiet_to=self.lbl_status)
        self._pending = rows or []
        self.tbl_pending.blockSignals(True)
        self.tbl_pending.setRowCount(0)
        for r in self._pending:
            add_row(self.tbl_pending, [r["product_name"], r["size"] or "", r["colour"] or "", r["qty"]], right={3})
        if self._pending:
            self.tbl_pending.setCurrentCell(min(max(keep, 0), len(self._pending) - 1), 0)
        self.tbl_pending.blockSignals(False)
        self._pending_selected()
        self.btn_fit.setEnabled(bool(self._pending))
        self.btn_damaged.setEnabled(bool(self._pending))

    def _pending_selected(self, *_) -> None:
        row = self.tbl_pending.currentRow()
        if 0 <= row < len(self._pending):
            self.spin_review.setRange(1, self._pending[row]["qty"])
            self.spin_review.setValue(self._pending[row]["qty"])
        else:
            self.spin_review.setRange(1, 1)

    def _review_fit(self, *_) -> None:
        self.review_selected(True)

    def _review_damaged(self, *_) -> None:
        self.review_selected(False)

    def review_selected(self, resellable: bool) -> None:
        """Decide what happens to returned pieces: fit for resale (sellable) or damaged."""
        if self._busy:
            return
        title = tr("Returned items to check")
        row = self.tbl_pending.currentRow()
        if not (0 <= row < len(self._pending)):
            self._error(title, tr("Choose the returned item first."))
            return
        item, qty = self._pending[row], self.spin_review.value()
        note = self.txt_review_note.text().strip() or None
        self._busy = True
        try:
            ok, _ = self._run(
                lambda: stock_ops.review_returned(self.conn, item["variant_id"], qty, resellable=resellable,
                                                  user_id=self.user_id, reason=note),
                title=title)
        finally:
            self._busy = False
        if not ok:
            return
        self.txt_review_note.clear()
        self._info(title, tr("{n} piece(s) can be sold again.", n=qty) if resellable
                   else tr("{n} piece(s) were put aside as damaged.", n=qty))
        self.reload_pending()