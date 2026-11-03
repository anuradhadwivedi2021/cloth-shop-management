# ui/suppliers_view.py
"""Suppliers screen: directory with what we owe each supplier, add, edit, disable / enable.

This screen only shows things and passes what was typed to core.supplier_admin /
core.supplier_ledger. It never touches the database itself, so permissions, GSTIN checks,
the audit log and transactions stay in the tested core code.
"""

import logging

from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGroupBox, QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.money import format_rupees
from core.permissions import has_permission
from core import supplier_admin, supplier_ledger
from core.supplier_admin import DuplicateSupplier
from ui.widgets import label

log = logging.getLogger("clothshop")

COLUMNS = ["ID", "Supplier", "Contact person", "Phone", "GSTIN", "State", "Status", "Balance"]


class SuppliersManagementView(QWidget):
    """Supplier directory and dues."""

    def __init__(self, parent=None, db_connection=None, user_id=None):
        super().__init__(parent)
        if db_connection is None or user_id is None:
            raise ValueError("The suppliers screen needs the open database and the signed-in user.")
        self.conn = db_connection
        self.user_id = user_id  # every action is done and logged as this user
        self.can_edit = has_permission(self.conn, self.user_id, "receive_purchase")
        self._suppliers = []

        self.setup_ui()
        self.load_suppliers()

    # ---------- layout ----------

    def setup_ui(self):
        layout = QHBoxLayout(self)

        # Left: add / edit form
        left_group = QGroupBox("Add / edit supplier")
        lg = QVBoxLayout()
        self.txt_name = QLineEdit()
        self.txt_contact = QLineEdit()
        self.txt_phone = QLineEdit()
        self.txt_email = QLineEdit()
        self.txt_gstin = QLineEdit()
        self.txt_state = QLineEdit()
        self.txt_address = QLineEdit()
        self.txt_gstin.setPlaceholderText("15 characters, e.g. 27AAPFU0939F1ZV (optional)")
        self.fields = (
            ("Supplier name:", self.txt_name), ("Contact person:", self.txt_contact), ("Phone number:", self.txt_phone),
            ("Email:", self.txt_email), ("GSTIN:", self.txt_gstin), ("State:", self.txt_state),
            ("Address:", self.txt_address),
        )
        for text, widget in self.fields:
            lg.addWidget(label(text))
            lg.addWidget(widget)

        self.btn_add = QPushButton("Add supplier")
        self.btn_add.setObjectName("primary")
        self.btn_add.clicked.connect(self.add_new_supplier)
        self.btn_save = QPushButton("Save changes to selected supplier")
        self.btn_save.clicked.connect(self.save_changes)
        self.btn_clear = QPushButton("Clear form")
        self.btn_clear.clicked.connect(self.clear_form)
        for button in (self.btn_add, self.btn_save, self.btn_clear):
            lg.addWidget(button)
        self.lbl_note = label("", "hint")
        lg.addWidget(self.lbl_note)
        lg.addStretch()
        left_group.setLayout(lg)

        # Right: search + table
        right_group = QGroupBox("Supplier directory and dues")
        rg = QVBoxLayout()
        top = QHBoxLayout()
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search by name, phone, GSTIN or contact person")
        self.txt_search.textChanged.connect(self.load_suppliers)
        self.chk_inactive = QCheckBox("Show disabled suppliers")
        self.chk_inactive.stateChanged.connect(self.load_suppliers)
        top.addWidget(self.txt_search, 1)
        top.addWidget(self.chk_inactive)
        rg.addLayout(top)

        self.table_suppliers = QTableWidget(0, len(COLUMNS))
        self.table_suppliers.setHorizontalHeaderLabels(COLUMNS)
        self.table_suppliers.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_suppliers.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_suppliers.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_suppliers.itemSelectionChanged.connect(self._selection_changed)
        rg.addWidget(self.table_suppliers)

        self.btn_toggle = QPushButton("Disable / Enable selected supplier")
        self.btn_toggle.clicked.connect(self.toggle_active)
        rg.addWidget(self.btn_toggle)
        rg.addWidget(label("Balance: a positive amount is what we owe the supplier.", "hint"))
        right_group.setLayout(rg)

        layout.addWidget(left_group, stretch=4)
        layout.addWidget(right_group, stretch=6)

        if not self.can_edit:
            for button in (self.btn_add, self.btn_save, self.btn_toggle):
                button.setEnabled(False)
            self.lbl_note.setText("You can view suppliers but not change them.")
        self._selection_changed()

    # ---------- showing data ----------

    @staticmethod
    def _balance_text(paise: int) -> str:
        return f"{format_rupees(-paise)} (supplier owes us)" if paise < 0 else format_rupees(paise)

    def load_suppliers(self, *_):
        try:
            self._suppliers = supplier_admin.list_suppliers(
                self.conn, self.user_id, query=self.txt_search.text().strip() or None,
                include_inactive=self.chk_inactive.isChecked(),
            )
        except ValueError as exc:  # includes "not allowed"
            self._suppliers = []
            QMessageBox.warning(self, "Suppliers", str(exc))
        except Exception:
            self._suppliers = []
            log.exception("E-SUPPLIERS-LIST: could not load suppliers")
            QMessageBox.critical(self, "Suppliers", "The supplier list could not be loaded. (Error code E-SUPPLIERS-LIST)")

        self.table_suppliers.setRowCount(0)
        for s in self._suppliers:
            row = self.table_suppliers.rowCount()
            self.table_suppliers.insertRow(row)
            values = [
                str(s["id"]), s["name"], s["contact_person"], s["phone"], s["gstin"], s["state"],
                "Active" if s["is_active"] else "Disabled", self._balance_text(s["balance_paise"]),
            ]
            for col, text in enumerate(values):
                self.table_suppliers.setItem(row, col, QTableWidgetItem(text))
        self._selection_changed()

    def _selected(self):
        row = self.table_suppliers.currentRow()
        return self._suppliers[row] if 0 <= row < len(self._suppliers) else None

    def _selection_changed(self, *_):
        chosen = self._selected()
        self.btn_save.setEnabled(self.can_edit and chosen is not None)
        self.btn_toggle.setEnabled(self.can_edit and chosen is not None)
        if chosen is not None:
            self.txt_name.setText(chosen["name"])
            self.txt_contact.setText(chosen["contact_person"])
            self.txt_phone.setText(chosen["phone"])
            self.txt_email.setText(chosen["email"])
            self.txt_gstin.setText(chosen["gstin"])
            self.txt_state.setText(chosen["state"])
            self.txt_address.setText(chosen["address"])

    def clear_form(self):
        for _text, widget in self.fields:
            widget.clear()
        self.table_suppliers.clearSelection()

    def _typed(self) -> dict:
        return {
            "contact_person": self.txt_contact.text(), "phone": self.txt_phone.text(), "email": self.txt_email.text(),
            "address": self.txt_address.text(), "gstin": self.txt_gstin.text(), "state": self.txt_state.text(),
        }

    # ---------- doing things (all through core) ----------

    def _run(self, action, success_text=None):
        """Run one action. Returns (saved, result). A success message is shown only if it really saved.

        A DuplicateSupplier warning is passed on to the caller, which asks the user.
        """
        try:
            result = action()
        except DuplicateSupplier:
            raise
        except ValueError as exc:  # includes PermissionDenied
            QMessageBox.warning(self, "Not saved", str(exc))
            return False, None
        except Exception:
            log.exception("E-SUPPLIERS-SAVE: supplier action failed")
            QMessageBox.critical(
                self, "Not saved", "Something went wrong and nothing was saved. (Error code E-SUPPLIERS-SAVE)"
            )
            return False, None
        if success_text:
            QMessageBox.information(self, "Done", success_text)
        return True, result

    def add_new_supplier(self):
        name = self.txt_name.text().strip()

        def create(confirm):
            return lambda: supplier_admin.add_supplier(
                self.conn, self.user_id, name, confirm_duplicate=confirm, **self._typed()
            )

        done = f"Supplier '{name}' was added."
        try:
            saved, _ = self._run(create(False), done)
        except DuplicateSupplier as dup:
            lines = "\n".join(f"- {m['name']} ({', '.join(m['reasons'])})" for m in dup.matches)
            answer = QMessageBox.question(
                self, "Supplier may already exist",
                f"These suppliers look the same:\n{lines}\n\nAdd '{name}' as a new supplier anyway?",
            )
            if answer != QMessageBox.Yes:
                return
            saved, _ = self._run(create(True), done)
        if saved:
            self.clear_form()
            self.load_suppliers()

    def save_changes(self):
        chosen = self._selected()
        if chosen is None:
            QMessageBox.information(self, "Suppliers", "Please select a supplier in the list first.")
            return
        saved, changed = self._run(
            lambda: supplier_admin.edit_supplier(
                self.conn, self.user_id, chosen["id"], name=self.txt_name.text(), **self._typed()
            )
        )
        if not saved:
            return
        if changed:
            QMessageBox.information(self, "Done", f"'{chosen['name']}' was updated. Old bills are not changed.")
            self.load_suppliers()
        else:
            QMessageBox.information(self, "Suppliers", "Nothing was changed.")

    def toggle_active(self):
        chosen = self._selected()
        if chosen is None:
            QMessageBox.information(self, "Suppliers", "Please select a supplier in the list first.")
            return
        make_active = not chosen["is_active"]
        verb = "enable" if make_active else "disable"
        if QMessageBox.question(self, "Please confirm", f"Do you want to {verb} '{chosen['name']}'?") != QMessageBox.Yes:
            return
        saved, _ = self._run(
            lambda: supplier_ledger.set_supplier_active(self.conn, self.user_id, chosen["id"], make_active),
            f"'{chosen['name']}' was {'enabled' if make_active else 'disabled'}. Purchase history is kept.",
        )
        if saved:
            self.load_suppliers()
