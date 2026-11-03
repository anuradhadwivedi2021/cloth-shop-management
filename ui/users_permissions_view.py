# ui/users_permissions_view.py
"""Users screen: list users, add a user, reset a password, disable / enable, unlock.

This screen only shows things and passes what was typed to core.user_admin.
It never touches the database itself, so permissions, password hashing, the
audit log and transactions all stay in the tested core code.
"""

import logging

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGroupBox, QHBoxLayout, QInputDialog, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import user_admin
from core.auth import AuthError
from ui.widgets import PasswordField, label

log = logging.getLogger("clothshop")

COLUMNS = ["User ID", "Username", "Name", "Role", "Status"]
ROLE_CHOICES = ["Cashier", "Stock staff", "Accountant", "Owner"]


class UsersAndPermissionsView(QWidget):
    """Users and roles (Owner, Cashier, Stock staff, Accountant)."""

    def __init__(self, parent=None, db_connection=None, user_id=None):
        super().__init__(parent)
        if db_connection is None or user_id is None:
            raise ValueError("The users screen needs the open database and the signed-in user.")
        self.conn = db_connection
        self.user_id = user_id  # the signed-in user: every action is done and logged as this user
        self._users = []

        self.setup_ui()
        self.load_users()

    # ---------- layout ----------

    def setup_ui(self):
        layout = QHBoxLayout(self)

        # Left: users table and actions for the selected user
        left_group = QGroupBox("Existing users and roles")
        lg_layout = QVBoxLayout()
        self.table_users = QTableWidget(0, len(COLUMNS))
        self.table_users.setHorizontalHeaderLabels(COLUMNS)
        self.table_users.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_users.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_users.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lg_layout.addWidget(self.table_users)

        actions = QHBoxLayout()
        self.btn_reset = QPushButton("Reset password")
        self.btn_toggle = QPushButton("Disable / Enable")
        self.btn_unlock = QPushButton("Unlock")
        self.btn_reset.clicked.connect(self.reset_password)
        self.btn_toggle.clicked.connect(self.toggle_active)
        self.btn_unlock.clicked.connect(self.unlock_user)
        for button in (self.btn_reset, self.btn_toggle, self.btn_unlock):
            actions.addWidget(button)
        lg_layout.addLayout(actions)
        left_group.setLayout(lg_layout)
        layout.addWidget(left_group, stretch=6)

        # Right: add a new user
        right_group = QGroupBox("Add a new user")
        rg_layout = QVBoxLayout()

        self.txt_username = QLineEdit()
        self.txt_username.setPlaceholderText("Username")
        self.txt_fullname = QLineEdit()
        self.txt_fullname.setPlaceholderText("Full name (optional)")
        self.txt_password = PasswordField("At least 8 characters")
        self.txt_password2 = PasswordField("Type the password again")
        self.cmb_role = QComboBox()
        self.cmb_role.addItems(ROLE_CHOICES)
        self.txt_pin = PasswordField("4 to 6 digits (optional)")

        btn_create = QPushButton("Add user")
        btn_create.setObjectName("primary")
        btn_create.clicked.connect(self.add_user)

        for text, widget in (
            ("Username:", self.txt_username),
            ("Full name:", self.txt_fullname),
            ("Password:", self.txt_password),
            ("Confirm password:", self.txt_password2),
            ("Role:", self.cmb_role),
            ("Owner override PIN (for discounts and return overrides):", self.txt_pin),
        ):
            rg_layout.addWidget(label(text))
            rg_layout.addWidget(widget)
        rg_layout.addWidget(btn_create)
        rg_layout.addStretch()
        right_group.setLayout(rg_layout)
        layout.addWidget(right_group, stretch=4)

    # ---------- showing data ----------

    def load_users(self):
        try:
            self._users = user_admin.list_users(self.conn, self.user_id)
        except ValueError as exc:  # includes "not allowed"
            self._users = []
            QMessageBox.warning(self, "Users", str(exc))
        except Exception:
            self._users = []
            log.exception("E-USERS-LIST: could not load users")
            QMessageBox.critical(self, "Users", "The user list could not be loaded. (Error code E-USERS-LIST)")

        self.table_users.setRowCount(0)
        for user in self._users:
            row = self.table_users.rowCount()
            self.table_users.insertRow(row)
            values = [str(user["id"]), user["username"], user["full_name"], user["role_label"], user["status"]]
            for col, text in enumerate(values):
                self.table_users.setItem(row, col, QTableWidgetItem(text))

    def _selected_user(self):
        row = self.table_users.currentRow()
        if row < 0 or row >= len(self._users):
            QMessageBox.information(self, "Users", "Please select a user in the list first.")
            return None
        return self._users[row]

    # ---------- doing things (all through core.user_admin) ----------

    def _run(self, action, success_text: str) -> bool:
        """Run one action. Success is shown only if it really saved."""
        try:
            action()
        except (ValueError, AuthError) as exc:  # PermissionDenied is a ValueError
            QMessageBox.warning(self, "Not saved", str(exc))
            return False
        except Exception:
            log.exception("E-USERS-SAVE: user action failed")
            QMessageBox.critical(
                self, "Not saved", "Something went wrong and nothing was saved. (Error code E-USERS-SAVE)"
            )
            return False
        QMessageBox.information(self, "Done", success_text)
        self.load_users()
        return True

    def add_user(self):
        username = self.txt_username.text().strip()
        password = self.txt_password.text()
        if password != self.txt_password2.text():
            QMessageBox.warning(self, "Not saved", "The two passwords are not the same.")
            return
        ok = self._run(
            lambda: user_admin.add_user(
                self.conn, self.user_id, username, password, self.cmb_role.currentText(),
                pin=self.txt_pin.text().strip() or None, full_name=self.txt_fullname.text().strip() or None,
            ),
            f"User '{username}' was added.",
        )
        if ok:
            for field in (self.txt_username, self.txt_fullname, self.txt_password, self.txt_password2, self.txt_pin):
                field.clear()

    def reset_password(self):
        user = self._selected_user()
        if user is None:
            return
        new_password, accepted = QInputDialog.getText(
            self, "Reset password", f"New password for '{user['username']}' (at least 8 characters):",
            QLineEdit.Password,
        )
        if not accepted:
            return
        self._run(
            lambda: user_admin.reset_password(self.conn, self.user_id, user["id"], new_password),
            f"The password for '{user['username']}' was changed.",
        )

    def toggle_active(self):
        user = self._selected_user()
        if user is None:
            return
        make_active = not user["is_active"]
        verb = "enable" if make_active else "disable"
        answer = QMessageBox.question(self, "Please confirm", f"Do you want to {verb} '{user['username']}'?")
        if answer != QMessageBox.Yes:
            return
        self._run(
            lambda: user_admin.set_active(self.conn, self.user_id, user["id"], make_active),
            f"'{user['username']}' was {'enabled' if make_active else 'disabled'}.",
        )

    def unlock_user(self):
        user = self._selected_user()
        if user is None:
            return
        self._run(
            lambda: user_admin.unlock(self.conn, self.user_id, user["id"]),
            f"'{user['username']}' was unlocked.",
        )