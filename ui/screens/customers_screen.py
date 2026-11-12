"""Customers menu: profiles with consent, credit and dues, history, privacy rights and reminders.

SRS 11 (customers), 14.1 (customer payments), CUS-01 to CUS-07 and the data-protection hooks of section 9.

No SQL and no rules here. core.customers (profiles, consent, export, erase, access log) and
core.customer_credit (credit terms, dues, receipts) check everything and write the audit log. A message says
"saved" only after the core call really returned, and technical detail of a fault goes to the log only.

Privacy by design:
  * Walk-in sales need no profile. A profile needs only a phone number and the customer's consent.
  * Opening a profile, its history, an export or the dues report is written to the customer access log by the
    core code, so the owner can see who looked at a customer's data.
  * Erasing personal data keeps only what tax records need; it is refused while money is still owed either way.
  * No lambdas are connected to signals (a lambda that holds the screen can crash Qt when it closes).
"""

import logging
import re

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout, QLineEdit, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from core import customer_credit as cc
from core import customers as cu
from core.money import format_rupees
from core.permissions import has_permission
from core.purchase_service import format_ui_date, parse_ui_date
from core.settings import get_setting
from ui.customer_credit_panel import CustomerCreditPanel
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import form, label

MENU_TEXT = "Customers"
log = logging.getLogger("clothshop")

ERASE_TEXT = {
    "customer_request": N_("The customer asked for it"),
    "consent_withdrawn": N_("Consent was withdrawn"),
    "retention_review": N_("Not active for a long time"),
    "other": N_("Other reason"),
}
BILL_STATUS = {"completed": N_("Completed"), "cancelled": N_("Cancelled")}
OUTCOME_TEXT = {"refund": N_("Refund"), "store_credit": N_("Store credit"), "exchange": N_("Exchange")}
FIELD_TEXT = {
    "name": N_("name"), "phone": N_("phone"), "address": N_("address"), "gstin": N_("GSTIN"),
    "birthday": N_("birthday"), "anniversary": N_("anniversary"), "preferred_size": N_("preferred size"), "notes": N_("notes"),
}


def birthday_to_core(text):
    """'25/03/2001' -> '2001-03-25'; '25/03' -> '03-25' (no year); anything else is passed on and checked by core."""
    value = (text or "").strip()
    full = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", value)
    if full:
        return parse_ui_date(value, tr("Birthday"))
    short = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", value)
    if short:
        return f"{int(short[2]):02d}-{int(short[1]):02d}"
    return value


def birthday_to_text(stored):
    """'2001-03-25' -> '25/03/2001'; '03-25' -> '25/03'."""
    if not stored:
        return ""
    if re.fullmatch(r"\d{2}-\d{2}", stored):
        return f"{stored[3:]}/{stored[:2]}"
    return format_ui_date(stored)


class NewCustomerDialog(QDialog):
    """Asks for a new customer's details and consent. values() returns what was typed."""

    def __init__(self, purpose, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("New customer"))
        self.txt_phone, self.txt_name, self.txt_address = QLineEdit(), QLineEdit(), QLineEdit()
        self.txt_gstin, self.txt_birthday, self.txt_size, self.txt_notes = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.txt_phone.setPlaceholderText(tr("10-digit phone number (required)"))
        self.txt_birthday.setPlaceholderText(tr("DD/MM/YYYY, or DD/MM without the year"))
        self.txt_anniversary = QLineEdit()
        self.txt_anniversary.setPlaceholderText(tr("DD/MM/YYYY, or DD/MM without the year"))
        self.chk_consent = QCheckBox(tr("The customer agrees to this"))
        rows = form()
        rows.addRow(tr("Phone"), self.txt_phone)
        rows.addRow(tr("Name"), self.txt_name)
        rows.addRow(tr("Address"), self.txt_address)
        rows.addRow(tr("GSTIN"), self.txt_gstin)
        rows.addRow(tr("Birthday"), self.txt_birthday)
        rows.addRow(tr("Anniversary"), self.txt_anniversary)
        rows.addRow(tr("Preferred size"), self.txt_size)
        rows.addRow(tr("Notes"), self.txt_notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        col = QVBoxLayout(self)
        col.addLayout(rows)
        col.addWidget(label(tr("Privacy notice: we keep this customer's details for:"), "hint"))
        col.addWidget(label(purpose))
        col.addWidget(self.chk_consent)
        col.addWidget(buttons)

    def values(self) -> dict:
        return dict(
            phone=self.txt_phone.text(), name=self.txt_name.text(), address=self.txt_address.text(),
            gstin=self.txt_gstin.text(), birthday=self.txt_birthday.text(), anniversary=self.txt_anniversary.text(), preferred_size=self.txt_size.text(),
            notes=self.txt_notes.text(), consent=self.chk_consent.isChecked(),
        )


class CustomersScreen(ScreenBase, QWidget):
    """Find, add and look after customers, take their payments and respect their data rights."""

    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        has = has_permission
        self.can_edit = has(conn, self.user_id, "edit_customers")
        self.can_data = has(conn, self.user_id, "manage_customer_data")
        self._rows = []          # the customers list
        self._customer = None    # the open profile (core.customers.get_customer)
        self._dues_rows = []
        self._busy = False

        self.lbl_status = label("", "error")
        self.tabs = QTabWidget()

        # ================= tab 1: customers =================
        page = QWidget()
        col = QVBoxLayout(page)
        top = QHBoxLayout()
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(tr("Search by name or phone number"))
        self.chk_archived = QCheckBox(tr("Show archived"))
        self.btn_new = QPushButton(tr("New customer"))
        top.addWidget(self.txt_search, 1)
        top.addWidget(self.chk_archived)
        top.addWidget(self.btn_new)
        col.addLayout(top)
        self.tbl_customers = make_table([tr("Name"), tr("Phone"), tr("Status")])
        col.addWidget(self.tbl_customers, 1)

        self.detail = QTabWidget()
        self.detail.addTab(self._build_profile_tab(), tr("Profile"))
        self.credit = CustomerCreditPanel(conn, session)
        self.detail.addTab(self.credit, tr("Credit and dues"))
        self.detail.addTab(self._build_history_tab(), tr("Bills and returns"))
        self.privacy_index = -1
        if self.can_data:
            self.privacy_index = self.detail.addTab(self._build_privacy_tab(), tr("Privacy"))
        col.addWidget(self.detail, 3)
        self.tabs.addTab(page, tr("Customers"))

        # ================= tab 2: dues report =================
        dues = QWidget()
        dc = QVBoxLayout(dues)
        dc.addWidget(label(tr("Opening this report is written to the customer access log, because it shows names "
                              "and phone numbers."), "hint"))
        row = QHBoxLayout()
        self.chk_overdue = QCheckBox(tr("Only overdue bills"))
        self.btn_dues = QPushButton(tr("Show dues report"))
        self.btn_open_due = QPushButton(tr("Open selected customer"))
        row.addWidget(self.chk_overdue)
        row.addWidget(self.btn_dues)
        row.addWidget(self.btn_open_due)
        row.addStretch(1)
        dc.addLayout(row)
        self.lbl_dues_total = label("", "title")
        dc.addWidget(self.lbl_dues_total)
        self.tbl_dues = make_table([
            tr("Customer"), tr("Phone"), tr("Open bills"), tr("Owes"), tr("Overdue"), tr("0–30 days"),
            tr("31–60 days"), tr("Over 60 days"),
        ])
        dc.addWidget(self.tbl_dues, 1)
        self.tabs.addTab(dues, tr("Who owes money"))

        # ================= tab 3: reminders =================
        rem = QWidget()
        rc = QVBoxLayout(rem)
        rc.addWidget(label(tr("Birthdays are only a list for you to call. Nothing is sent to anyone."), "hint"))
        brow = QHBoxLayout()
        self.spin_days = QSpinBox()
        self.spin_days.setRange(1, 60)
        self.spin_days.setValue(7)
        brow.addWidget(label(tr("Birthdays in the next (days)")))
        brow.addWidget(self.spin_days)
        brow.addStretch(1)
        rc.addLayout(brow)
        self.tbl_birthdays = make_table([tr("Name"), tr("Phone"), tr("Occasion"), tr("Date"), tr("In (days)")])
        rc.addWidget(self.tbl_birthdays, 1)
        self.tbl_inactive = make_table([tr("Name"), tr("Phone"), tr("Last activity"), tr("Store credit")])
        if self.can_data:
            rc.addWidget(label(tr("Customers with no activity for a long time. Review them; nothing is erased "
                                  "automatically."), "hint"))
            rc.addWidget(self.tbl_inactive, 1)
        self.tabs.addTab(rem, tr("Reminders"))

        outer = QVBoxLayout(self)
        outer.addWidget(label(tr("Customers"), "title"))
        outer.addWidget(self.lbl_status)
        outer.addWidget(self.tabs, 1)

        self.txt_search.textChanged.connect(self.reload_customers)
        self.chk_archived.stateChanged.connect(self.reload_customers)
        self.btn_new.clicked.connect(self.new_customer)
        self.tbl_customers.currentCellChanged.connect(self._customer_selected)
        self.detail.currentChanged.connect(self._detail_tab_changed)
        self.credit.changed.connect(self._credit_changed)
        self.btn_dues.clicked.connect(self.show_dues)
        self.btn_open_due.clicked.connect(self.open_due_customer)
        self.spin_days.valueChanged.connect(self.reload_birthdays)
        self.btn_new.setEnabled(self.can_edit)
        self._set_detail_enabled()
        self.refresh()

    # ------------------------------------------------------------ building the detail tabs

    def _build_profile_tab(self) -> QWidget:
        tab = QWidget()
        col = QVBoxLayout(tab)
        self.edit_name, self.edit_phone, self.edit_address = QLineEdit(), QLineEdit(), QLineEdit()
        self.edit_gstin, self.edit_birthday, self.edit_size, self.edit_notes = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.edit_birthday.setPlaceholderText(tr("DD/MM/YYYY, or DD/MM without the year"))
        self.edit_anniversary = QLineEdit()
        self.edit_anniversary.setPlaceholderText(tr("DD/MM/YYYY, or DD/MM without the year"))
        rows = form()
        rows.addRow(tr("Name"), self.edit_name)
        rows.addRow(tr("Phone"), self.edit_phone)
        rows.addRow(tr("Address"), self.edit_address)
        rows.addRow(tr("GSTIN"), self.edit_gstin)
        rows.addRow(tr("Birthday"), self.edit_birthday)
        rows.addRow(tr("Anniversary"), self.edit_anniversary)
        rows.addRow(tr("Preferred size"), self.edit_size)
        rows.addRow(tr("Notes"), self.edit_notes)
        col.addLayout(rows)
        self.lbl_consent = label("", "hint")
        self.lbl_money = label("", "title")
        col.addWidget(self.lbl_consent)
        col.addWidget(self.lbl_money)
        buttons = QHBoxLayout()
        self.btn_save = QPushButton(tr("Save details"))
        self.btn_save.setObjectName("primary")
        self.btn_consent = QPushButton(tr("Customer agrees again"))
        self.btn_withdraw = QPushButton(tr("Customer withdrew consent"))
        self.btn_archive = QPushButton(tr("Archive customer"))
        for b in (self.btn_save, self.btn_consent, self.btn_withdraw, self.btn_archive):
            buttons.addWidget(b)
        col.addLayout(buttons)
        col.addStretch(1)
        self.btn_save.clicked.connect(self.save_profile)
        self.btn_consent.clicked.connect(self.record_consent)
        self.btn_withdraw.clicked.connect(self.withdraw_consent)
        self.btn_archive.clicked.connect(self.toggle_archive)
        return tab

    def _build_history_tab(self) -> QWidget:
        tab = QWidget()
        col = QVBoxLayout(tab)
        self.lbl_history = label("", "title")
        col.addWidget(self.lbl_history)
        self.tbl_bills = make_table([tr("Bill"), tr("Date"), tr("Status"), tr("Total"), tr("Paid"), tr("Still due")])
        self.tbl_returns = make_table([tr("Credit note"), tr("Date"), tr("What happened"), tr("Total"), tr("Store credit")])
        col.addWidget(label(tr("Bills"), "hint"))
        col.addWidget(self.tbl_bills, 2)
        col.addWidget(label(tr("Returns and exchanges"), "hint"))
        col.addWidget(self.tbl_returns, 1)
        return tab

    def _build_privacy_tab(self) -> QWidget:
        tab = QWidget()
        col = QVBoxLayout(tab)
        col.addWidget(label(tr("The customer can ask for a copy of their data, or for it to be erased. Bills stay: "
                               "tax records need them."), "hint"))
        box = QGroupBox(tr("Data rights"))
        bc = QHBoxLayout(box)
        self.btn_export = QPushButton(tr("Save a copy of this customer's data"))
        self.cmb_erase = QComboBox()
        for key in cu.ERASE_REASONS:
            self.cmb_erase.addItem(tr(ERASE_TEXT[key]), key)
        self.btn_erase = QPushButton(tr("Erase personal data"))
        bc.addWidget(self.btn_export)
        bc.addWidget(self.cmb_erase)
        bc.addWidget(self.btn_erase)
        col.addWidget(box)
        col.addWidget(label(tr("Who has looked at this customer's data"), "hint"))
        self.tbl_access = make_table([tr("What"), tr("Who"), tr("When")])
        col.addWidget(self.tbl_access, 1)
        self.btn_export.clicked.connect(self.export_data)
        self.btn_erase.clicked.connect(self.erase_data)
        return tab

    # ------------------------------------------------------------ small pop-ups (tests replace these)

    def _ask_save_path(self, default_name):
        path, _ = QFileDialog.getSaveFileName(self, tr("Save a copy of the data"), default_name, "JSON (*.json)")
        return path or None

    def _confirm_text(self):
        return get_setting(self.conn, "customer_consent_purpose") or cu.DEFAULT_CONSENT_PURPOSE

    # ------------------------------------------------------------ loading

    def refresh(self) -> None:
        """Called each time the page is opened."""
        self.reload_customers()
        self.reload_birthdays()
        self.reload_inactive()

    def reload_customers(self, *_, select_id=None) -> None:
        keep = select_id if select_id is not None else (self._customer["id"] if self._customer else None)
        ok, rows = self._run(
            lambda: cu.search_customers(self.conn, self.txt_search.text(), user_id=self.user_id,
                                        include_inactive=self.chk_archived.isChecked(), limit=200),
            title=tr("Customers"), quiet_to=self.lbl_status)
        self._rows = rows or []
        self.tbl_customers.blockSignals(True)
        self.tbl_customers.setRowCount(0)
        for r in self._rows:
            add_row(self.tbl_customers, [r["name"] or tr("(no name)"), r["phone"],
                                         tr("Active") if r["is_active"] else tr("Archived")])
        index = next((i for i, r in enumerate(self._rows) if r["id"] == keep), -1)
        self.tbl_customers.setCurrentCell(index, 0 if index >= 0 else -1)
        self.tbl_customers.blockSignals(False)
        self._customer_selected()

    def _customer_selected(self, *_) -> None:
        row = self.tbl_customers.currentRow()
        customer_id = self._rows[row]["id"] if 0 <= row < len(self._rows) else None
        self._open_profile(customer_id)

    def _open_profile(self, customer_id) -> None:
        """Open one customer. The core code writes this to the access log."""
        self._customer = None
        if customer_id is not None:
            ok, data = self._run(lambda: cu.get_customer(self.conn, customer_id, user_id=self.user_id),
                                 title=tr("Customers"), quiet_to=self.lbl_status)
            if ok:
                self._customer = data
        self._paint_profile()
        self.credit.set_customer(self._customer["id"] if self._customer else None)
        self._load_detail_tab()

    def _paint_profile(self) -> None:
        c = self._customer
        for edit, key in ((self.edit_name, "name"), (self.edit_phone, "phone"), (self.edit_address, "address"),
                          (self.edit_gstin, "gstin"), (self.edit_size, "preferred_size"), (self.edit_notes, "notes")):
            edit.setText((c[key] or "") if c else "")
        self.edit_birthday.setText(birthday_to_text(c["birthday"]) if c else "")
        self.edit_anniversary.setText(birthday_to_text(c["anniversary"]) if c else "")
        if c is None:
            self.lbl_consent.setText("")
            self.lbl_money.setText("")
        else:
            if c["consent_given"]:
                self.lbl_consent.setText(tr("Consent given on {date}. Purpose: {purpose}",
                                            date=format_ui_date((c["consent_date"] or "")[:10]),
                                            purpose=c["consent_purpose"] or ""))
            else:
                self.lbl_consent.setText(tr("No consent on record. This profile is archived and cannot be used on bills."))
            self.lbl_money.setText(tr("Store credit: {amount}", amount=format_rupees(c["store_credit_paise"])))
            self.btn_archive.setText(tr("Archive customer") if c["is_active"] else tr("Bring customer back"))
        self._set_detail_enabled()

    def _set_detail_enabled(self) -> None:
        has = self._customer is not None and not self._busy
        for edit in (self.edit_name, self.edit_phone, self.edit_address, self.edit_gstin, self.edit_birthday,
                     self.edit_anniversary, self.edit_size, self.edit_notes):
            edit.setEnabled(has and self.can_edit)
        self.btn_save.setEnabled(has and self.can_edit)
        self.btn_consent.setEnabled(has and self.can_edit)
        self.btn_withdraw.setEnabled(has and self.can_edit and bool(self._customer and self._customer["consent_given"]))
        self.btn_archive.setEnabled(has and self.can_edit)
        if self.can_data:
            self.btn_export.setEnabled(has)
            self.btn_erase.setEnabled(has)
            self.cmb_erase.setEnabled(has)

    def _detail_tab_changed(self, *_) -> None:
        self._load_detail_tab()

    def _load_detail_tab(self) -> None:
        """History and the access log are read only when their tab is open (each read is logged by core)."""
        index = self.detail.currentIndex()
        if self._customer is None:
            self.tbl_bills.setRowCount(0)
            self.tbl_returns.setRowCount(0)
            self.lbl_history.setText("")
            if self.can_data:
                self.tbl_access.setRowCount(0)
            return
        if index == 2:
            self._load_history()
        elif self.can_data and index == self.privacy_index:
            self._load_access()

    def _load_history(self) -> None:
        ok, h = self._run(lambda: cu.customer_history(self.conn, self._customer["id"], user_id=self.user_id),
                          title=tr("Customers"), quiet_to=self.lbl_status)
        self.tbl_bills.setRowCount(0)
        self.tbl_returns.setRowCount(0)
        if not ok:
            return
        self.lbl_history.setText(tr("Owes {owes}   Store credit {credit}", owes=format_rupees(h["outstanding_paise"]),
                                    credit=format_rupees(h["store_credit_paise"])))
        for b in h["bills"]:
            add_row(self.tbl_bills, [
                b["invoice_no"], format_ui_date((b["date"] or "")[:10]), tr(BILL_STATUS.get(b["status"], b["status"])),
                format_rupees(b["total_paise"]), format_rupees(b["paid_paise"]), format_rupees(b["due_paise"]),
            ], right={3, 4, 5})
        for r in h["returns"]:
            add_row(self.tbl_returns, [
                r["credit_note_no"], format_ui_date((r["created_at"] or "")[:10]),
                tr(OUTCOME_TEXT.get(r["outcome"], r["outcome"])), format_rupees(r["total_paise"]),
                format_rupees(r["store_credit_paise"]),
            ], right={3, 4})

    def _load_access(self) -> None:
        ok, entries = self._run(lambda: cu.list_access_log(self.conn, self._customer["id"], user_id=self.user_id),
                                title=tr("Privacy"), quiet_to=self.lbl_status)
        self.tbl_access.setRowCount(0)
        for e in entries or []:
            add_row(self.tbl_access, [e["action"], e["user"] or "", (e["at"] or "")[:16]])

    def _credit_changed(self) -> None:
        """A payment or credit change was saved: refresh the money line and the history."""
        if self._customer is not None:
            self._open_profile(self._customer["id"])

    # ------------------------------------------------------------ new customer

    def new_customer(self, *_) -> None:
        dialog = NewCustomerDialog(self._confirm_text(), self)
        if dialog.exec() == QDialog.Accepted:
            self.add_customer(**dialog.values())

    def add_customer(self, phone, name="", address="", gstin="", birthday="", preferred_size="", notes="",
                     consent=False, anniversary="") -> bool:
        """Save a new profile. Needs the customer's consent. Returns True when it was saved."""
        if self._busy:
            return False
        title = tr("New customer")
        fields = dict(name=name, address=address, gstin=gstin, preferred_size=preferred_size, notes=notes)
        try:
            fields["birthday"] = birthday_to_core(birthday)
            fields["anniversary"] = birthday_to_core(anniversary)
        except ValueError as exc:
            self._error(title, str(exc))
            return False
        new_id = self._create(title, phone, fields, consent, allow_duplicate=False)
        if new_id is None:
            return False
        self.txt_search.blockSignals(True)
        self.txt_search.clear()
        self.txt_search.blockSignals(False)
        self.reload_customers(select_id=new_id)
        self._info(title, tr("The customer was saved."))
        return True

    def _create(self, title, phone, fields, consent, *, allow_duplicate):
        self._busy = True
        try:
            try:
                return cu.create_customer(self.conn, phone, user_id=self.user_id, consent_given=bool(consent),
                                          allow_duplicate_phone=allow_duplicate, **fields)
            except cu.DuplicateCustomerError as exc:
                existing = exc.existing_id
            except Exception as exc:  # noqa: BLE001 - every failure becomes a short message
                self._show_failure(title, exc)
                return None
        finally:
            self._busy = False
        # A customer with this phone number exists: open that one, or confirm it is a different person.
        if self._ask(title, tr("A customer with this phone number already exists. Open that profile instead?")):
            self.reload_customers(select_id=existing)
            return None
        if self._ask(title, tr("Save this as a different person with the same phone number (for example a shared "
                               "family phone)?")):
            return self._create(title, phone, fields, consent, allow_duplicate=True)
        return None

    def _keep_visible(self) -> None:
        """An archived customer is hidden from the list; keep it showing so the owner can still work with it
        (record consent again, erase the data, bring it back)."""
        self.chk_archived.blockSignals(True)
        self.chk_archived.setChecked(True)
        self.chk_archived.blockSignals(False)

    def _show_failure(self, title, exc) -> None:
        from core.purchase_service import safe_message
        text, expected = safe_message(exc)
        if not expected:
            log.exception("Customer screen action failed (%s)", title)
        self._error(title, tr(text))

    # ------------------------------------------------------------ the open profile

    def save_profile(self, *_) -> None:
        if self._busy or self._customer is None:
            return
        title = tr("Customers")
        try:
            birthday = birthday_to_core(self.edit_birthday.text())
            anniversary = birthday_to_core(self.edit_anniversary.text())
        except ValueError as exc:
            self._error(title, str(exc))
            return
        cid = self._customer["id"]
        ok, changed = self._run(
            lambda: cu.update_customer(
                self.conn, cid, user_id=self.user_id, name=self.edit_name.text(), phone=self.edit_phone.text(),
                address=self.edit_address.text(), gstin=self.edit_gstin.text(), birthday=birthday, anniversary=anniversary,
                preferred_size=self.edit_size.text(), notes=self.edit_notes.text()),
            title=title)
        if not ok:
            return
        if changed:
            names = ", ".join(tr(FIELD_TEXT.get(k, k)) for k in changed)
            self._info(title, tr("Saved. Changed: {fields}.", fields=names))
        else:
            self._info(title, tr("Nothing was changed."))
        self.reload_customers(select_id=cid)

    def record_consent(self, *_) -> None:
        if self._busy or self._customer is None:
            return
        title = tr("Consent")
        cid = self._customer["id"]
        if not self._ask(title, tr("The customer agrees to: {purpose}. Record this?", purpose=self._confirm_text())):
            return
        ok, _ = self._run(lambda: cu.record_consent(self.conn, cid, user_id=self.user_id), title=title)
        if ok:
            self._info(title, tr("Consent recorded. The profile is active."))
            self.reload_customers(select_id=cid)

    def withdraw_consent(self, *_) -> None:
        if self._busy or self._customer is None:
            return
        title = tr("Consent")
        cid = self._customer["id"]
        if not self._ask(title, tr("The customer withdrew consent. The profile will be archived and cannot be used on "
                                   "new bills. Continue?")):
            return
        ok, _ = self._run(lambda: cu.withdraw_consent(self.conn, cid, user_id=self.user_id), title=title)
        if ok:
            self._info(title, tr("Consent withdrawn and the profile archived. You can now erase the personal data "
                                 "if the customer asks."))
            self._keep_visible()
            self.reload_customers(select_id=cid)

    def toggle_archive(self, *_) -> None:
        if self._busy or self._customer is None:
            return
        title = tr("Customers")
        cid, make_active = self._customer["id"], not self._customer["is_active"]
        ok, _ = self._run(lambda: cu.set_customer_active(self.conn, cid, make_active, user_id=self.user_id), title=title)
        if ok:
            if not make_active:
                self._keep_visible()
            self.reload_customers(select_id=cid)

    # ------------------------------------------------------------ privacy rights

    def export_data(self, *_) -> None:
        """Save everything stored about the customer as a file (a copy for that customer only)."""
        if self._busy or self._customer is None:
            return
        title = tr("Privacy")
        cid = self._customer["id"]
        path = self._ask_save_path(f"customer-{cid}-data.json")
        if not path:
            return
        ok, saved = self._run(lambda: cu.export_customer_data_to_file(self.conn, cid, path, user_id=self.user_id),
                              title=title)
        if ok:
            self._info(title, tr("Saved to {path}. The file holds personal data: give it only to this customer.",
                                 path=saved))
            self._load_access()

    def erase_data(self, *_) -> None:
        """Erase the personal data, keeping only what tax records need."""
        if self._busy or self._customer is None:
            return
        title = tr("Privacy")
        cid, reason = self._customer["id"], self.cmb_erase.currentData()
        question = tr("Erase the personal data of this customer? The phone number, address, birthday, size, notes and "
                      "consent are removed for good. Bills stay. This cannot be undone.")
        if not self._ask(title, question):
            return
        self._busy = True
        try:
            ok, result = self._run(lambda: cu.erase_customer(self.conn, cid, user_id=self.user_id, reason=reason),
                                   title=title)
        finally:
            self._busy = False
        if not ok:
            self._set_detail_enabled()
            return
        text = tr("The personal data was erased.")
        if result["kept_for_tax"]:
            text += " " + tr("The name and GSTIN are kept because tax invoices must show them (ask your accountant).")
        self._info(title, text)
        self.txt_search.blockSignals(True)
        self.txt_search.clear()
        self.txt_search.blockSignals(False)
        self.reload_customers()
        self.reload_inactive()

    # ------------------------------------------------------------ dues report and reminders

    def show_dues(self, *_) -> None:
        ok, report = self._run(lambda: cc.dues_report(self.conn, user_id=self.user_id,
                                                      overdue_only=self.chk_overdue.isChecked()),
                               title=tr("Who owes money"))
        self.tbl_dues.setRowCount(0)
        self._dues_rows = []
        if not ok:
            self.lbl_dues_total.setText("")
            return
        self._dues_rows = report["rows"]
        t = report["totals"]
        self.lbl_dues_total.setText(tr("{n} customer(s) owe {total}, of which {overdue} is overdue",
                                       n=t["customers"], total=format_rupees(t["total_paise"]),
                                       overdue=format_rupees(t["overdue_paise"])))
        for r in self._dues_rows:
            add_row(self.tbl_dues, [
                r["name"] or tr("(no name)"), r["phone"], r["open_bills"], format_rupees(r["outstanding_paise"]),
                format_rupees(r["overdue_paise"]), format_rupees(r["bucket_0_30_paise"]),
                format_rupees(r["bucket_31_60_paise"]), format_rupees(r["bucket_over_60_paise"]),
            ], right={2, 3, 4, 5, 6, 7})

    def open_due_customer(self, *_) -> None:
        row = self.tbl_dues.currentRow()
        if not (0 <= row < len(self._dues_rows)):
            self._error(tr("Who owes money"), tr("Select a customer first."))
            return
        self.txt_search.blockSignals(True)
        self.txt_search.clear()
        self.txt_search.blockSignals(False)
        self.reload_customers(select_id=self._dues_rows[row]["customer_id"])
        self.tabs.setCurrentIndex(0)
        self.detail.setCurrentIndex(1)

    def reload_birthdays(self, *_) -> None:
        ok, rows = self._run(lambda: cu.upcoming_birthdays(self.conn, user_id=self.user_id, days=self.spin_days.value()),
                             title=tr("Reminders"), quiet_to=self.lbl_status)
        self.tbl_birthdays.setRowCount(0)
        for r in rows or []:
            when = tr("today") if r["days_until"] == 0 else r["days_until"]
            occasion = tr("Birthday") if r["occasion"] == "birthday" else tr("Anniversary")
            add_row(self.tbl_birthdays, [r["name"] or tr("(no name)"), r["phone"], occasion,
                                         birthday_to_text(r["birthday"]), when], right={4})

    def reload_inactive(self) -> None:
        if not self.can_data:
            return
        ok, rows = self._run(lambda: cu.inactive_customers(self.conn, user_id=self.user_id),
                             title=tr("Reminders"), quiet_to=self.lbl_status)
        self.tbl_inactive.setRowCount(0)
        for r in rows or []:
            add_row(self.tbl_inactive, [r["name"] or tr("(no name)"), r["phone"],
                                        format_ui_date((r["last_activity"] or "")[:10]),
                                        format_rupees(r["store_credit_paise"])], right={3})


def build(conn, session, folder):
    return CustomersScreen(conn, session)