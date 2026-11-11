"""The billing counter (SRS 5.4): scan or search, pick size x colour, hold bills, split payments, save.

Rules this screen follows (they are why it was rebuilt):
  * A bill is saved ONLY by core.guarded.sell (permission check, discount limit + owner PIN, one
    database transaction, stock reduced once). The screen runs no SQL.
  * "Saved" is shown only after sell() returned a sale id. Every failure shows an error, keeps the
    cart as it was, and saves nothing.
  * One bill attempt has one idempotency key, so a double-click or a retry can never save twice.
    The key changes when the cart, customer, bill type, discount or held slot changes.
  * Printing happens after saving, through ui.print_service.print_bill. A failed print never undoes
    the sale; the message says the bill is saved and can be reprinted.
  * Keyboard first (scanner = keyboard input): F2 scan/search, F3 customer, F4 bill type, F5 hold,
    F6 held bills, F7 bill discount, F8 line discount, F9 quantity, F11 clear, F12 pay and save,
    Del remove line, + / - change quantity, Esc back to the scan box.
"""

import logging
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import bill_preview as bp
from core import wallet as wallet_core
from core.auth import AuthError
from core.customers import get_customer, search_customers
from core.guarded import sell
from core.money import format_rupees, parse_rupees
from core.permissions import PermissionDenied, discount_cap_percent, has_permission
from core.products import search_products
from core.sales import (
    ACCEPTED_PAYMENT_MODES, discard_held_bill, enabled_payment_modes, get_sale, hold_bill, list_held_bills,
    resume_held_bill,
)
from core.session import SessionLocked
from core.settings import get_bool
from core.shop_settings import billing_problems, get_shop_profile, place_of_supply
from core.stock import InsufficientStockError
from core.upi_qr import qr_for_bill
from ui import print_service
from ui.i18n import N_, tr
from ui.widgets import label

log = logging.getLogger(__name__)

MODE_LABELS = {
    "cash": N_("Cash"), "upi": N_("UPI"), "card": N_("Card"),
    "bank_transfer": N_("Bank transfer"), "cheque": N_("Cheque"), "other": N_("Other"),
}
_PERCENT = re.compile(r"^\d+(\.\d{1,2})?$")


def _plain(paise: int) -> str:
    """Paise -> '1050.50' for an input box (exact, no float)."""
    return f"{paise // 100}.{paise % 100:02d}"


def parse_discount(text: str, basis_paise: int) -> int:
    """'150' / '150.50' = rupees, '10%' = percent of basis. Returns paise. ValueError is safe to show."""
    text = (text or "").strip()
    if not text:
        return 0
    if text.endswith("%"):
        number = text[:-1].strip()
        if not _PERCENT.match(number) or Decimal(number) > 100:
            raise ValueError(tr("Enter a discount percent between 0 and 100, for example 10%."))
        return int((Decimal(basis_paise) * Decimal(number) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return parse_rupees(text)


# ======================================================================= dialogs

class VariantPickerDialog(QDialog):
    """Size x colour grid with the stock count in every cell. Out-of-stock cells cannot be chosen."""

    def __init__(self, conn, product_id, product_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Choose size and colour - {name}", name=product_name))
        self.variant_id, self.qty = None, 1
        allow_negative = get_bool(conn, "allow_negative_stock")
        variants = bp.variants_of_product(conn, product_id)
        sizes = list(dict.fromkeys(v["size"] for v in variants))
        colours = list(dict.fromkeys(v["colour"] for v in variants))
        self._stock = {v["variant_id"]: v["sellable"] > 0 or allow_negative for v in variants}

        self.grid = QTableWidget(len(sizes), len(colours))
        self.grid.setHorizontalHeaderLabels(colours)
        self.grid.setVerticalHeaderLabels(sizes)
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for row in range(len(sizes)):
            self.grid.setRowHeight(row, 52)
        for v in variants:
            if self._stock[v["variant_id"]]:
                text = tr("{n} in stock\n{price}", n=v["sellable"], price=format_rupees(v["price_paise"]))
            else:
                text = tr("Out of stock\n{price}", price=format_rupees(v["price_paise"]))
            item = QTableWidgetItem(text)
            item.setData(Qt.UserRole, v["variant_id"])
            item.setTextAlignment(Qt.AlignCenter)
            if not self._stock[v["variant_id"]]:
                item.setForeground(Qt.gray)
            self.grid.setItem(sizes.index(v["size"]), colours.index(v["colour"]), item)
        self.grid.itemActivated.connect(self._pick)  # Enter or double-click

        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 999)
        self.message = label("", "error")
        self.add_btn = QPushButton(tr("Add to bill"))
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(lambda: self._pick(self.grid.currentItem()))
        row = QHBoxLayout()
        row.addWidget(label(tr("Quantity")))
        row.addWidget(self.qty_spin)
        row.addStretch(1)
        row.addWidget(self.add_btn)
        col = QVBoxLayout(self)
        col.addWidget(label(tr("Use the arrow keys, then Enter. Grey cells are out of stock."), "hint"))
        col.addWidget(self.grid, 1)
        col.addWidget(self.message)
        col.addLayout(row)
        self.resize(640, 420)
        self.grid.setFocus()

    def _pick(self, item):
        if item is None:
            self.message.setText(tr("Choose a size and colour first."))
            return
        variant_id = item.data(Qt.UserRole)
        if not self._stock.get(variant_id):
            self.message.setText(tr("That size and colour is out of stock. Choose another cell."))
            return
        self.variant_id, self.qty = variant_id, self.qty_spin.value()
        self.accept()


class CustomerDialog(QDialog):
    """Pick a saved customer, or walk-in (no details needed)."""

    def __init__(self, conn, user_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Customer"))
        self.conn, self.user_id = conn, user_id
        self.customer_id, self.customer_name = None, None
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Type a name or phone number"))
        self.list = QListWidget()
        self.message = label("", "hint")
        self.search.textChanged.connect(self._fill)
        self.search.returnPressed.connect(lambda: self.list.setFocus())
        self.list.itemActivated.connect(self._pick)
        col = QVBoxLayout(self)
        col.addWidget(self.search)
        col.addWidget(self.list, 1)
        col.addWidget(self.message)
        self.resize(460, 380)
        self._fill("")

    def _fill(self, text):
        self.list.clear()
        walk_in = QListWidgetItem(tr("Walk-in customer (no details)"))
        walk_in.setData(Qt.UserRole, None)
        self.list.addItem(walk_in)
        try:
            rows = search_customers(self.conn, text, user_id=self.user_id, limit=30)
            self.message.setText(tr("Press Enter in the search box to move to the list, Enter again to choose."))
        except PermissionDenied:
            rows = []
            self.message.setText(tr("You are not allowed to view customers, so only walk-in billing is available."))
        for r in rows:
            name = r["name"] or tr("(no name)")
            item = QListWidgetItem(f"{name}  -  {r['phone']}")
            item.setData(Qt.UserRole, r["id"])
            item.setData(Qt.UserRole + 1, name)
            self.list.addItem(item)
        self.list.setCurrentRow(0)

    def _pick(self, item):
        self.customer_id = item.data(Qt.UserRole)
        self.customer_name = item.data(Qt.UserRole + 1)
        self.accept()


class HeldBillsDialog(QDialog):
    """Parked bills (up to 10). Enter resumes, Delete discards."""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Held bills"))
        self.conn, self.slot = conn, None
        self.list = QListWidget()
        self.message = label(tr("Enter = resume this bill.   Delete = discard it."), "hint")
        self.list.itemActivated.connect(self._pick)
        QShortcut(QKeySequence(Qt.Key_Delete), self.list, activated=self._discard, context=Qt.WidgetShortcut)
        col = QVBoxLayout(self)
        col.addWidget(self.list, 1)
        col.addWidget(self.message)
        self.resize(460, 340)
        self._fill()

    def _fill(self):
        self.list.clear()
        for h in list_held_bills(self.conn):
            note = f" - {h['note']}" if h["note"] else ""
            item = QListWidgetItem(tr("Slot {slot}: {n} item(s), held {when}{note}",
                                      slot=h["slot"], n=h["items"], when=h["created_at"], note=note))
            item.setData(Qt.UserRole, h["slot"])
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.message.setText(tr("No bills are on hold."))

    def _pick(self, item):
        self.slot = item.data(Qt.UserRole)
        self.accept()

    def _discard(self):
        item = self.list.currentItem()
        if item is None:
            return
        answer = QMessageBox.question(self, tr("Discard held bill"), tr("Discard this held bill? It cannot be brought back."))
        if answer != QMessageBox.Yes:
            return
        try:
            discard_held_bill(self.conn, item.data(Qt.UserRole))
        except ValueError as exc:
            self.message.setText(str(exc))
        self._fill()


class PaymentDialog(QDialog):
    """Up to three payment lines (split payment). The screen's save callback does the real saving.

    on_save(payments) -> (ok, message). While saving, and after a successful save, every further
    click is ignored (second line of defence after the idempotency key).
    """

    ROWS = 3

    def __init__(self, total_paise, on_save, *, credit_ok=False, has_customer=False, modes=ACCEPTED_PAYMENT_MODES, upi=None,
                 wallet=None, voucher_balance=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Payment"))
        self.total, self._on_save, self._busy = total_paise, on_save, False
        self._credit_ok, self._has_customer = credit_ok, has_customer
        self._upi = upi            # BIL-13: {'upi_id', 'png', ...} or None when the shop has no UPI id
        self._wallet = wallet      # CUS-05/06: {'points', 'point_value_paise', 'store_credit_paise', ...} or None
        self._voucher_balance = voucher_balance      # code -> the amount that voucher can pay now, or None
        grid = QGridLayout()
        self.rows = []
        for i in range(self.ROWS):
            combo = QComboBox()
            for mode in modes:
                combo.addItem(tr(MODE_LABELS.get(mode, mode)), mode)
            amount = QLineEdit(_plain(total_paise) if i == 0 else "")
            amount.setPlaceholderText(tr("Amount"))
            ref = QLineEdit()
            ref.setPlaceholderText(tr("Reference (optional)"))
            amount.textChanged.connect(self._update)
            amount.returnPressed.connect(self.submit)
            ref.returnPressed.connect(self.submit)
            grid.addWidget(combo, i, 0)
            grid.addWidget(amount, i, 1)
            grid.addWidget(ref, i, 2)
            self.rows.append((combo, amount, ref))
        self.pts_edit, self.credit_edit, self.voucher_edit = QLineEdit(), QLineEdit(), QLineEdit()
        wallet_box = QGridLayout()
        self.wallet_widgets = []
        if wallet is not None:
            if wallet.get("loyalty_enabled") and wallet.get("points", 0) > 0:
                self.pts_edit.setPlaceholderText(tr("Points to use (you have {n})", n=wallet["points"]))
                wallet_box.addWidget(self.pts_edit, 0, 0)
                self.wallet_widgets.append(self.pts_edit)
            if wallet.get("store_credit_paise", 0) > 0:
                self.credit_edit.setPlaceholderText(tr("Store credit to use (Rs, you have {amount})",
                                                       amount=format_rupees(wallet["store_credit_paise"])))
                wallet_box.addWidget(self.credit_edit, 0, 1)
                self.wallet_widgets.append(self.credit_edit)
        if voucher_balance is not None:
            self.voucher_edit.setPlaceholderText(tr("Gift voucher code"))
            wallet_box.addWidget(self.voucher_edit, 1, 0, 1, 2)
            self.wallet_widgets.append(self.voucher_edit)
        for edit in self.wallet_widgets:
            edit.textChanged.connect(self._wallet_changed)
            edit.returnPressed.connect(self.submit)
        self.total_label = label(tr("Bill total: {amount}", amount=format_rupees(total_paise)), "title")
        self.balance = label("")
        self.error = label("", "error")
        self.save_btn = QPushButton(tr("Save bill"))
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.submit)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.upi_btn = QPushButton(tr("Show UPI QR"))
        self.upi_btn.setVisible(upi is not None)
        self.upi_btn.clicked.connect(self.toggle_upi)
        self.upi_label = QLabel()
        self.upi_label.setAlignment(Qt.AlignCenter)
        self.upi_label.setVisible(False)
        self.upi_note = label("", "hint")
        self.upi_note.setVisible(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.upi_btn)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_btn)
        col = QVBoxLayout(self)
        col.addWidget(self.total_label)
        if self.wallet_widgets:
            col.addLayout(wallet_box)
        col.addLayout(grid)
        col.addWidget(self.balance)
        col.addWidget(self.upi_label)
        col.addWidget(self.upi_note)
        col.addWidget(self.error)
        col.addLayout(buttons)
        self.resize(640, 300)
        self._update()
        self.rows[0][1].setFocus()
        self.rows[0][1].selectAll()

    def toggle_upi(self):
        """Show or hide the shop's UPI QR for this bill. The app cannot know if the customer paid: the cashier
        confirms the money arrived, then types it in as a UPI payment above."""
        show = not self.upi_label.isVisible()
        if show and self._upi:
            pixmap = QPixmap()
            pixmap.loadFromData(self._upi["png"], "PNG")
            self.upi_label.setPixmap(pixmap.scaled(260, 260, Qt.KeepAspectRatio, Qt.FastTransformation))
            self.upi_note.setText(tr("Customer scans and pays {amount} to {upi}. When the money has arrived, enter it "
                                     "above as a UPI payment.", amount=format_rupees(self.total), upi=self._upi["upi_id"]))
        self.upi_label.setVisible(show)
        self.upi_note.setVisible(show)
        self.upi_btn.setText(tr("Hide UPI QR") if show else tr("Show UPI QR"))
        self.adjustSize()

    def payments(self) -> list:
        result = []
        for combo, amount, ref in self.rows:
            text = amount.text().strip()
            if text:
                result.append({
                    "mode": combo.currentData(),
                    "amount_paise": parse_rupees(text, allow_zero=False),
                    "reference": ref.text().strip() or None,
                })
        return result

    def tenders(self) -> list:
        """Points, store credit and a gift voucher the customer wants to pay with (checked again by the core)."""
        result = []
        pts = self.pts_edit.text().strip()
        if pts:
            if not pts.isdigit() or int(pts) <= 0:
                raise ValueError(tr("Points must be a whole number."))
            result.append({"kind": "loyalty", "points": int(pts)})
        credit = self.credit_edit.text().strip()
        if credit:
            result.append({"kind": "store_credit", "amount_paise": parse_rupees(credit, allow_zero=False)})
        code = self.voucher_edit.text().strip()
        if code:
            result.append({"kind": "voucher", "code": code})
        return result

    def _wallet_paise(self) -> int:
        """What the typed points, credit and voucher are worth (for the figures shown; never more than the bill)."""
        try:
            tenders = self.tenders()
        except ValueError:
            return 0
        total = 0
        for t in tenders:
            if t["kind"] == "loyalty":
                total += t["points"] * self._wallet["point_value_paise"]
            elif t["kind"] == "store_credit":
                total += t["amount_paise"]
            elif self._voucher_balance is not None:
                total += self._voucher_balance(t["code"]) or 0
        return min(total, self.total)

    def _wallet_changed(self, *_):
        """Offer the rest of the bill as the money to collect, unless the cashier already split it."""
        if all(not self.rows[i][1].text().strip() for i in range(1, self.ROWS)):
            self.rows[0][1].setText(_plain(self.total - self._wallet_paise()))
        self._update()

    def _update(self):
        try:
            tendered = sum(p["amount_paise"] for p in self.payments()) + (self._wallet_paise() if self._wallet else 0)
        except ValueError as exc:
            self.balance.setText(str(exc))
            return
        if tendered >= self.total:
            self.balance.setText(tr("Change to give back: {amount}", amount=format_rupees(tendered - self.total)))
        else:
            short = format_rupees(self.total - tendered)
            if self._credit_ok and self._has_customer:
                self.balance.setText(tr("Amount due (credit sale): {amount}", amount=short))
            else:
                self.balance.setText(tr("Still to collect: {amount}", amount=short))

    def submit(self):
        if self._busy:
            return
        self._busy = True
        self.save_btn.setEnabled(False)
        self.error.setText("")
        try:
            self.tenders()           # a typo in the points or store credit box is reported before anything is saved
            ok, message = self._on_save(self.payments())
        except ValueError as exc:
            ok, message = False, str(exc)
        except Exception:  # the screen's callback handles its own errors; this is the last safety net
            log.exception("Payment dialog: unexpected error while saving")
            ok, message = False, tr("The bill could not be saved. Nothing was changed.")
        if ok:
            self.accept()  # stays busy: a second click does nothing
            return
        self._busy = False
        self.save_btn.setEnabled(True)
        self.error.setText(message)


# ======================================================================= the screen

class BillingCounterView(QWidget):
    sale_saved = Signal(int)

    def __init__(self, conn, session, folder=None, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.folder = conn, session, folder
        self.user_id = session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.cart = []  # [{"variant_id", "qty", "discount_paise"}]
        self.customer_id, self.customer_name = None, None
        self.held_slot = None
        self.last_sale_id = None
        self._preview, self._bill_error, self._bill_discount = None, None, 0
        self._place = {"intra_state": True, "label": "CGST + SGST", "warning": None}
        self._saving = False
        self._fp, self._attempt_key = None, uuid.uuid4().hex
        self._shortcuts = []

        self._build()
        self._update_place()
        self.refresh()

    # ------------------------------------------------------------ layout

    def _build(self):
        self.scan = QLineEdit()
        self.scan.setPlaceholderText(tr("Scan barcode, or type name / code, then Enter   (F2)"))
        self.scan.returnPressed.connect(lambda: self.submit_code(self.scan.text()))
        self.scan.textChanged.connect(self._live_search)
        self.results = QListWidget()
        self.results.setMaximumHeight(130)
        self.results.hide()
        self.results.itemActivated.connect(self._result_chosen)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([tr("Item"), tr("Qty"), tr("Price"), tr("Discount"), tr("GST %"), tr("Amount")])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.itemActivated.connect(lambda _item: self.change_qty_dialog())

        left = QVBoxLayout()
        left.addWidget(self.scan)
        left.addWidget(self.results)
        left.addWidget(self.table, 1)

        # right-hand panel
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(370)
        side = QVBoxLayout(card)
        side.setContentsMargins(18, 16, 18, 16)
        side.setSpacing(8)

        self.mode_combo = QComboBox()
        scheme = get_shop_profile(self.conn)["tax_scheme"]
        self.mode_combo.addItem(tr("Bill of supply (composition)") if scheme == "composition" else tr("GST tax invoice"), "gst")
        self.mode_combo.addItem(tr("Simple bill (no GST)"), "simple")
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._recalc())
        self.customer_label = label(tr("Walk-in customer"), None)
        self.discount_edit = QLineEdit()
        self.discount_edit.setPlaceholderText(tr("Bill discount: 150 or 10%   (F7)"))
        self.discount_edit.editingFinished.connect(self._recalc)
        self.held_label = label("", "hint")

        grid = QGridLayout()
        self.v_subtotal, self.v_discount, self.v_tax = QLabel(), QLabel(), QLabel()
        self.v_round, self.v_total = QLabel(), QLabel()
        self.tax_caption = QLabel()
        rows = [(QLabel(tr("Items total")), self.v_subtotal), (QLabel(tr("Discount")), self.v_discount),
                (self.tax_caption, self.v_tax), (QLabel(tr("Round off")), self.v_round)]
        for i, (cap, val) in enumerate(rows):
            val.setAlignment(Qt.AlignRight)
            grid.addWidget(cap, i, 0)
            grid.addWidget(val, i, 1)
        self.v_total.setObjectName("title")
        self.v_total.setAlignment(Qt.AlignRight)

        self.problem = label("", "error")
        self.status = label("", "hint")
        self.pay_btn = QPushButton(tr("Pay and save   (F12)"))
        self.pay_btn.setObjectName("primary")
        self.pay_btn.clicked.connect(self.pay_and_save)
        self.hold_btn = QPushButton(tr("Hold bill   (F5)"))
        self.hold_btn.clicked.connect(self.hold_current)
        self.held_btn = QPushButton(tr("Held bills   (F6)"))
        self.held_btn.clicked.connect(self.show_held)
        self.clear_btn = QPushButton(tr("Clear bill   (F11)"))
        self.clear_btn.clicked.connect(lambda: self.clear_bill(confirm=True))
        self.cust_btn = QPushButton(tr("Customer   (F3)"))
        self.cust_btn.clicked.connect(self.pick_customer)

        side.addWidget(label(tr("Bill type   (F4)"), "hint"))
        side.addWidget(self.mode_combo)
        side.addWidget(self.customer_label)
        side.addWidget(self.cust_btn)
        side.addWidget(self.discount_edit)
        side.addSpacing(6)
        side.addLayout(grid)
        side.addWidget(self.v_total)
        side.addWidget(self.problem)
        side.addWidget(self.pay_btn)
        row = QHBoxLayout()
        row.addWidget(self.hold_btn)
        row.addWidget(self.held_btn)
        side.addLayout(row)
        side.addWidget(self.clear_btn)
        side.addWidget(self.held_label)
        side.addStretch(1)

        top = QHBoxLayout()
        top.addLayout(left, 1)
        top.addWidget(card)
        keys = label(
            tr("F2 scan/search · F3 customer · F4 bill type · F5 hold · F6 held bills · F7 discount · F8 line discount · "
               "F9 quantity · F11 clear · F12 pay and save · Del remove line · +/- quantity · Esc scan box"), "hint")
        col = QVBoxLayout(self)
        col.setContentsMargins(24, 20, 24, 16)
        col.addWidget(label(tr("New Sale / Billing"), "title"))
        col.addLayout(top, 1)
        col.addWidget(self.status)
        col.addWidget(keys)

        self.setTabOrder(self.scan, self.table)
        self._shortcut("F2", self._focus_scan)
        self._shortcut("Escape", self._focus_scan)
        self._shortcut("F3", self.pick_customer)
        self._shortcut("F4", self.toggle_mode)
        self._shortcut("F5", self.hold_current)
        self._shortcut("F6", self.show_held)
        self._shortcut("F7", lambda: (self.discount_edit.setFocus(), self.discount_edit.selectAll()))
        self._shortcut("F8", self.line_discount_dialog)
        self._shortcut("F9", self.change_qty_dialog)
        self._shortcut("F11", lambda: self.clear_bill(confirm=True))
        self._shortcut("F12", self.pay_and_save)
        self._shortcut("Delete", self.remove_selected, self.table, Qt.WidgetShortcut)
        self._shortcut("+", lambda: self._nudge(1), self.table, Qt.WidgetShortcut)
        self._shortcut("-", lambda: self._nudge(-1), self.table, Qt.WidgetShortcut)
        self._shortcut("Down", self._to_results, self.scan, Qt.WidgetShortcut)

    def _shortcut(self, key, fn, widget=None, context=Qt.WidgetWithChildrenShortcut):
        sc = QShortcut(QKeySequence(key), widget or self)
        sc.setContext(context)
        sc.activated.connect(fn)
        self._shortcuts.append(sc)

    # ------------------------------------------------------------ small helpers (dialogs are replaceable in tests)

    def _say(self, text, kind="hint"):
        self.status.setText(text)
        self.status.setObjectName(kind)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _ask_int(self, title, text, value, low, high):
        number, ok = QInputDialog.getInt(self, title, text, value, low, high)
        return number if ok else None

    def _ask_text(self, title, text):
        value, ok = QInputDialog.getText(self, title, text)
        return value if ok else None

    def _ask_pin(self, percent, cap):
        text = tr("This discount is {percent:.1f}%, above your limit of {cap:g}%. Ask the owner to type the PIN:",
                  percent=percent, cap=cap)
        pin, ok = QInputDialog.getText(self, tr("Owner PIN"), text, QLineEdit.Password)
        return pin if ok and pin else None

    def _confirm(self, title, text) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def _run_picker(self, product_id, name):
        dlg = VariantPickerDialog(self.conn, product_id, name, self)
        return (dlg.variant_id, dlg.qty) if dlg.exec() == QDialog.Accepted else (None, 0)

    def _focus_scan(self):
        self.results.hide()
        self.scan.setFocus()
        self.scan.selectAll()

    def _bill_mode(self) -> str:
        if self.mode_combo.currentData() == "simple":
            return "unregistered"
        scheme = get_shop_profile(self.conn)["tax_scheme"]
        return scheme if scheme in ("regular", "composition") else "regular"

    def toggle_mode(self):
        self.mode_combo.setCurrentIndex(1 - self.mode_combo.currentIndex())

    def _cart_lines(self) -> list:
        return [{"variant_id": l["variant_id"], "qty": l["qty"], "discount_paise": l["discount_paise"]} for l in self.cart]

    # ------------------------------------------------------------ finding products (scanner = keyboard)

    def _live_search(self, text):
        text = text.strip()
        if len(text) < 3 or text.isdigit():  # a scanned barcode is digits: no pop-up while it arrives
            self.results.hide()
            return
        self._show_results(text)

    def _show_results(self, text) -> int:
        self.results.clear()
        for p in search_products(self.conn, text, limit=20):
            item = QListWidgetItem(p["name"] + (f"  ({p['brand']})" if p["brand"] else ""))
            item.setData(Qt.UserRole, p["id"])
            item.setData(Qt.UserRole + 1, p["name"])
            self.results.addItem(item)
        self.results.setVisible(self.results.count() > 0)
        return self.results.count()

    def _to_results(self):
        if self.results.isVisible() and self.results.count():
            self.results.setFocus()
            self.results.setCurrentRow(0)

    def submit_code(self, text) -> None:
        """Enter in the scan box: an exact barcode/SKU adds the item; otherwise search by name."""
        text = (text or "").strip()
        if not text:
            return
        info = bp.find_by_code(self.conn, text)
        if info is not None:
            if self.add_variant(info["variant_id"]):
                self.scan.clear()
            return
        count = self._show_results(text)
        if count == 0:
            self._say(tr("Nothing found for '{typed}'.", typed=text), "error")
        elif count == 1:
            self._result_chosen(self.results.item(0))
        else:
            self._say(tr("{n} products found. Use the Down arrow, then Enter to choose one.", n=count))

    def _result_chosen(self, item):
        self.pick_variant(item.data(Qt.UserRole), item.data(Qt.UserRole + 1))

    def pick_variant(self, product_id, name) -> bool:
        variant_id, qty = self._run_picker(product_id, name)
        if variant_id is None:
            self._focus_scan()
            return False
        added = self.add_variant(variant_id, qty)
        if added:
            self.scan.clear()
            self._focus_scan()
        return added

    # ------------------------------------------------------------ the cart

    def _line(self, variant_id):
        return next((l for l in self.cart if l["variant_id"] == variant_id), None)

    def _stock_short(self, info, wanted):
        """A message when `wanted` pieces of this variant are not in stock, else None."""
        if get_bool(self.conn, "allow_negative_stock") or wanted <= info["sellable"]:
            return None
        if info["sellable"] <= 0:
            text = tr("{item} is out of stock.", item=info["label"])
        else:
            text = tr("{item}: only {n} in stock.", item=info["label"], n=info["sellable"])
        alts = bp.alternatives(self.conn, info["variant_id"])
        if alts:
            text += " " + tr("Available instead: {list}.", list=", ".join(f"{a['size']} {a['colour']} ({a['sellable']})" for a in alts))
        return text

    def add_variant(self, variant_id, qty=1) -> bool:
        """Put an item on the bill. Blocked (with a reason) when it is inactive, unpriced or not in stock."""
        info = bp.variant_info(self.conn, variant_id)
        if info is None:
            self._say(tr("That product was not found."), "error")
            return False
        if not info["is_active"]:
            self._say(tr("{item} is inactive and cannot be sold.", item=info["label"]), "error")
            return False
        if info["price_paise"] <= 0:
            self._say(tr("{item} has no selling price. Set the price first.", item=info["label"]), "error")
            return False
        line = self._line(variant_id)
        wanted = (line["qty"] if line else 0) + qty
        short = self._stock_short(info, wanted)
        if short:
            self._say(short, "error")
            return False
        if line:
            line["qty"] = wanted
        else:
            self.cart.append({"variant_id": variant_id, "qty": qty, "discount_paise": 0})
        self._recalc()
        self.table.selectRow(len(self.cart) - 1 if not line else self.cart.index(line))
        self._say(tr("Added {item}.", item=info["label"]))
        return True

    def set_qty(self, row, qty) -> bool:
        if not 0 <= row < len(self.cart) or not isinstance(qty, int) or qty < 1:
            self._say(tr("Quantity must be a whole number, at least 1."), "error")
            return False
        info = bp.variant_info(self.conn, self.cart[row]["variant_id"])
        short = self._stock_short(info, qty)
        if short:
            self._say(short, "error")
            return False
        self.cart[row]["qty"] = qty
        self._recalc()
        return True

    def set_line_discount(self, row, text) -> bool:
        if not 0 <= row < len(self.cart):
            return False
        if (text or "").strip() and not has_permission(self.conn, self.user_id, "discount"):
            self._say(tr("You are not allowed to give discounts."), "error")
            return False
        line = self.cart[row]
        info = bp.variant_info(self.conn, line["variant_id"])
        gross = info["price_paise"] * line["qty"]
        try:
            paise = parse_discount(text, gross)
        except ValueError as exc:
            self._say(str(exc), "error")
            return False
        if paise > gross:
            self._say(tr("The discount cannot be more than the item amount."), "error")
            return False
        line["discount_paise"] = paise
        self._recalc()
        return True

    def remove_line(self, row) -> bool:
        if not 0 <= row < len(self.cart):
            return False
        del self.cart[row]
        self._recalc()
        return True

    def _selected_row(self) -> int:
        row = self.table.currentRow()
        return row if row >= 0 else (len(self.cart) - 1 if self.cart else -1)

    def remove_selected(self):
        self.remove_line(self._selected_row())

    def _nudge(self, delta):
        row = self._selected_row()
        if row >= 0 and self.cart[row]["qty"] + delta >= 1:
            self.set_qty(row, self.cart[row]["qty"] + delta)

    def change_qty_dialog(self):
        row = self._selected_row()
        if row < 0:
            return
        qty = self._ask_int(tr("Quantity"), tr("New quantity:"), self.cart[row]["qty"], 1, 999)
        if qty is not None:
            self.set_qty(row, qty)

    def line_discount_dialog(self):
        row = self._selected_row()
        if row < 0:
            return
        text = self._ask_text(tr("Item discount"), tr("Discount for this item (150 or 10%):"))
        if text is not None:
            self.set_line_discount(row, text)

    def clear_bill(self, confirm=False) -> bool:
        if confirm and self.cart and not self._confirm(tr("Clear bill"), tr("Remove all items from this bill?")):
            return False
        self._reset_bill()
        self._say(tr("Bill cleared."))
        return True

    def _reset_bill(self):
        self.cart = []
        self.held_slot = None
        self.discount_edit.clear()
        self._set_customer(None)
        self._fp, self._attempt_key = None, uuid.uuid4().hex
        self._recalc()

    # ------------------------------------------------------------ customer and place of supply

    def _update_place(self, gstin=None):
        try:
            self._place = place_of_supply(self.conn, customer_gstin=gstin)
        except ValueError as exc:
            self._say(str(exc), "error")
            self._place = place_of_supply(self.conn)

    def _set_customer(self, customer_id, name=None) -> bool:
        gstin = None
        if customer_id is not None:
            try:
                profile = get_customer(self.conn, customer_id, user_id=self.user_id)
                name, gstin = profile["name"] or name, profile["gstin"]
            except PermissionDenied:
                name = name or tr("customer #{id}", id=customer_id)
        try:
            self._place = place_of_supply(self.conn, customer_gstin=gstin)
        except ValueError as exc:  # a saved GSTIN that is not valid: do not guess the tax split
            self._say(str(exc), "error")
            return False
        self.customer_id, self.customer_name = customer_id, name
        text = tr("Customer: {name}", name=name or tr("(no name)")) if customer_id is not None else tr("Walk-in customer")
        self.customer_label.setText(text)
        self._recalc()
        return True

    def pick_customer(self):
        dlg = CustomerDialog(self.conn, self.user_id, self)
        if dlg.exec() == QDialog.Accepted:
            self._set_customer(dlg.customer_id, dlg.customer_name)
        self._focus_scan()

    # ------------------------------------------------------------ totals

    def _recalc(self):
        """Work out the bill with the same functions the save uses. Shows a problem instead of guessing."""
        self._bill_error, self._preview, self._bill_discount = None, None, 0
        mode = self._bill_mode()
        self.table.setColumnHidden(4, mode != "regular")
        if self.cart:
            try:
                problems = billing_problems(self.conn, mode)
                if problems:
                    raise ValueError(" ".join(problems))
                intra = self._place["intra_state"] if mode == "regular" else True
                base = bp.preview_bill(self.conn, self._cart_lines(), mode=mode, intra_state=intra)
                calc = base["calc"]
                discount = parse_discount(self.discount_edit.text(), calc["gross_paise"] - calc["item_discount_paise"])
                self._preview = base if discount == 0 else bp.preview_bill(
                    self.conn, self._cart_lines(), mode=mode, intra_state=intra, bill_discount_paise=discount)
                self._bill_discount = discount
            except ValueError as exc:
                self._bill_error = str(exc)
        fingerprint = (tuple((l["variant_id"], l["qty"], l["discount_paise"]) for l in self.cart),
                       self.customer_id, mode, self.discount_edit.text().strip(), self.held_slot)
        if fingerprint != self._fp:  # the bill changed: it is a different attempt, so it gets a new key
            self._fp, self._attempt_key = fingerprint, uuid.uuid4().hex
        self._paint()

    def _paint(self):
        keep = self.table.currentRow()
        preview = self._preview
        self.table.setRowCount(len(self.cart))
        for r, line in enumerate(self.cart):
            if preview:
                p = preview["lines"][r]
                cells = [p["label"], str(p["qty"]), format_rupees(p["unit_price_paise"]),
                         format_rupees(p["discount_paise"]) if p["discount_paise"] else "",
                         f"{p['tax_rate_percent']:g}", format_rupees(p["line_total_paise"])]
            else:
                info = bp.variant_info(self.conn, line["variant_id"])
                cells = [info["label"] if info else "?", str(line["qty"]), "", "", "", ""]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        if self.cart:
            self.table.selectRow(min(max(keep, 0), len(self.cart) - 1) if keep >= 0 else len(self.cart) - 1)

        zero = format_rupees(0)
        if preview:
            c = preview["calc"]
            self.v_subtotal.setText(format_rupees(c["gross_paise"]))
            self.v_discount.setText(format_rupees(c["item_discount_paise"] + c["bill_discount_paise"]))
            self.v_round.setText(format_rupees(c["round_off_paise"]))
            self.v_total.setText(format_rupees(c["total_paise"]))
            if c["mode"] == "regular":
                self.tax_caption.setText(tr("GST ({label})", label=self._place["label"]))
                self.v_tax.setText(format_rupees(c["tax_paise"]))
            else:
                self.tax_caption.setText(tr("GST"))
                self.v_tax.setText(tr("not charged"))
        else:
            for v in (self.v_subtotal, self.v_discount, self.v_round, self.v_total):
                v.setText(zero)
            self.tax_caption.setText(tr("GST"))
            self.v_tax.setText(zero)
        hint = self._place["warning"] if self.cart and self._bill_mode() == "regular" and not self._bill_error else None
        self.problem.setText(self._bill_error or hint or "")
        self.pay_btn.setEnabled(bool(self.cart) and preview is not None)
        self.hold_btn.setEnabled(bool(self.cart))

    def refresh(self):
        """Called each time the page is opened."""
        self._update_place(None if self.customer_id is None else self._gstin_or_none())
        held = len(list_held_bills(self.conn))
        self.held_label.setText(tr("{n} bill(s) on hold", n=held))
        self._recalc()
        self._focus_scan()

    def _gstin_or_none(self):
        try:
            return get_customer(self.conn, self.customer_id, user_id=self.user_id)["gstin"]
        except PermissionDenied:
            return None

    # ------------------------------------------------------------ hold and resume

    def hold_current(self) -> bool:
        if not has_permission(self.conn, self.user_id, "hold_bills"):
            self._say(tr("You are not allowed to hold bills."), "error")
            return False
        if not self.cart:
            self._say(tr("There is nothing to hold."), "error")
            return False
        try:
            slot = hold_bill(self.conn, self._cart_lines(), user_id=self.user_id,
                             customer_id=self.customer_id, bill_discount_paise=self._bill_discount)
        except ValueError as exc:
            self._say(str(exc), "error")
            return False
        self._reset_bill()
        self.refresh()
        self._say(tr("Bill held in slot {slot}. Press F6 to resume it.", slot=slot), "ok")
        return True

    def show_held(self):
        dlg = HeldBillsDialog(self.conn, self)
        if dlg.exec() == QDialog.Accepted and dlg.slot is not None:
            self.resume_held(dlg.slot)
        self.held_label.setText(tr("{n} bill(s) on hold", n=len(list_held_bills(self.conn))))
        self._focus_scan()

    def resume_held(self, slot) -> bool:
        if not has_permission(self.conn, self.user_id, "hold_bills"):
            self._say(tr("You are not allowed to resume held bills."), "error")
            return False
        if self.cart:
            self._say(tr("Hold or clear the current bill before resuming another."), "error")
            return False
        try:
            data = resume_held_bill(self.conn, slot)
        except ValueError as exc:
            self._say(str(exc), "error")
            return False
        self.cart = [{"variant_id": l["variant_id"], "qty": l["qty"], "discount_paise": l.get("discount_paise") or 0}
                     for l in data["cart"]]
        self.held_slot = slot
        self.discount_edit.setText(_plain(data["bill_discount_paise"]) if data["bill_discount_paise"] else "")
        self._set_customer(data["customer_id"])
        problems = self._stock_problems()
        if problems:
            self._say(tr("Bill resumed, but: {problems}", problems=" ".join(problems)), "error")
        else:
            self._say(tr("Held bill {slot} resumed.", slot=slot), "ok")
        return True

    # ------------------------------------------------------------ pay and save

    def _stock_problems(self) -> list:
        if get_bool(self.conn, "allow_negative_stock"):
            return []
        problems = []
        for line in self.cart:
            info = bp.variant_info(self.conn, line["variant_id"])
            if info is not None and line["qty"] > info["sellable"]:
                problems.append(self._stock_short(info, line["qty"]))
        return problems

    def pay_and_save(self):
        if not has_permission(self.conn, self.user_id, "sell"):
            self._say(tr("You are not allowed to make a sale."), "error")
            return
        self._recalc()
        if not self.cart:
            self._say(tr("Add at least one item first."), "error")
            return
        if self._preview is None:
            self._say(self._bill_error or tr("This bill cannot be saved yet."), "error")
            return
        problems = self._stock_problems()
        if problems:
            self._say(" ".join(problems), "error")
            return
        holder = []
        wallet = {"points": 0, "store_credit_paise": 0, "loyalty_enabled": False,
                  "point_value_paise": wallet_core.loyalty_settings(self.conn)["point_value_paise"]}
        if self.customer_id is not None:
            try:
                wallet.update(wallet_core.balances(self.conn, self.customer_id))
            except Exception:
                log.exception("Billing counter: could not read the customer's points and credit")
        dlg = PaymentDialog(
            self._preview["total_paise"], lambda payments: self.save_bill(payments, holder[0].tenders()),
            credit_ok=get_bool(self.conn, "credit_sales_enabled"), has_customer=self.customer_id is not None,
            modes=enabled_payment_modes(self.conn), upi=self._upi_for(self._preview["total_paise"]),
            wallet=wallet, voucher_balance=self._voucher_amount, parent=self,
        )
        holder.append(dlg)
        dlg.exec()
        self._focus_scan()

    def _voucher_amount(self, code):
        """What a gift voucher can pay now (0 when the code is unknown, used up, expired or cancelled)."""
        try:
            view = wallet_core.get_voucher(self.conn, code)
        except ValueError:
            return 0
        return view["balance_paise"] if view["usable"] else 0

    def _upi_for(self, total_paise):
        """The UPI QR for this total, or None when it is switched off or the shop has no valid UPI id."""
        if not get_bool(self.conn, "show_upi_qr_on_bill", True) or total_paise <= 0:
            return None
        try:
            return qr_for_bill(get_shop_profile(self.conn), total_paise)
        except ValueError:
            return None

    def save_bill(self, payments, tenders=None) -> tuple:
        """Save the current bill. Returns (ok, message). 'Saved' is reported only after sell() succeeded."""
        if self._saving:
            return False, tr("Saving is already in progress.")
        self._saving = True
        try:
            return self._save_bill(payments, tenders)
        finally:
            self._saving = False

    def _fail(self, message) -> tuple:
        self._say(message, "error")
        return False, message

    def _save_bill(self, payments, tenders=None) -> tuple:
        try:
            self.session.touch()
        except SessionLocked:
            return self._fail(tr("The screen is locked. Sign in again; this bill was not saved."))
        self._recalc()
        if not self.cart:
            return self._fail(tr("Add at least one item first."))
        if self._preview is None:
            return self._fail(self._bill_error or tr("This bill cannot be saved yet."))
        problems = self._stock_problems()
        if problems:
            return self._fail(" ".join(problems))

        calc = self._preview["calc"]
        pin = None
        discount = calc["item_discount_paise"] + calc["bill_discount_paise"]
        if discount > 0:
            percent = discount * 100 / calc["gross_paise"]
            cap = discount_cap_percent(self.conn, self.user_id)
            if percent > cap + 1e-9:
                pin = self._ask_pin(percent, cap)
                if not pin:
                    return self._fail(tr("This discount is above your limit. The owner's PIN is needed to save the bill."))

        try:
            sale_id = sell(
                self.conn, self._cart_lines(), payments, user_id=self.user_id, approver_pin=pin,
                customer_id=self.customer_id, mode=self._bill_mode(),
                intra_state=self._place["intra_state"] if self._bill_mode() == "regular" else True,
                bill_discount_paise=self._bill_discount, idempotency_key=self._attempt_key, held_slot=self.held_slot,
                tenders=tenders or None,
            )
        except (ValueError, AuthError, InsufficientStockError) as exc:  # PermissionDenied is a ValueError
            return self._fail(str(exc))
        except Exception:
            log.exception("Billing counter: the bill could not be saved")
            return self._fail(tr("The bill could not be saved. Nothing was changed. "
                                 "You can press Save again; it cannot be saved twice."))

        return True, self._after_saved(sale_id)

    def _after_saved(self, sale_id) -> str:
        try:
            invoice_no = get_sale(self.conn, sale_id)["sale"]["invoice_no"]
        except Exception:
            invoice_no = f"#{sale_id}"
        self.last_sale_id = sale_id
        self._reset_bill()  # a new bill gets a new key; the saved one can never be saved again
        try:
            result = print_service.print_bill(self.conn, self.session, sale_id, parent=self)
            printed, note = result.ok, result.message
        except Exception:
            log.exception("Billing counter: printing failed after the bill was saved")
            printed, note = False, tr("The printer reported a problem.")
        if printed:
            message = tr("Bill {invoice} saved. {note}", invoice=invoice_no, note=note)
        else:
            message = tr("Bill {invoice} saved. It was NOT printed: {note} You can reprint it later; this will not create another sale.",
                         invoice=invoice_no, note=note)
        self._say(message, "ok")
        self.held_label.setText(tr("{n} bill(s) on hold", n=len(list_held_bills(self.conn))))
        self.sale_saved.emit(sale_id)
        return message