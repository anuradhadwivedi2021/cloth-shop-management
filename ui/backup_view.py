# ui/backup_view.py
"""Backup and Restore screen: the two named buttons from the SRS, wired to core/backup.py.

This screen contains no database code. Every action goes through the tested functions in
core/backup.py, so permissions, the audit log, integrity checks, the safety backup before a
restore and "never lower a bill counter" are all enforced there, not here.
"""

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.paths import data_folder
from core import backup
from core.backup import BackupError
from core.permissions import PermissionDenied, has_permission
from core.session import SessionLocked
from core.settings import get_setting, set_setting
from ui.widgets import PasswordField, form, label

log = logging.getLogger("clothshop")

FOLDER_SETTING = "backup_folder"
_FMT = "%Y-%m-%d %H:%M:%S"


def pretty_time(stamp) -> str:
    """'2026-11-03 18:30:05' -> '03/11/2026 18:30:05' (DD/MM/YYYY, 24-hour clock)."""
    try:
        return datetime.strptime(stamp, _FMT).strftime("%d/%m/%Y %H:%M:%S")
    except (TypeError, ValueError):
        return str(stamp or "-")


def rupees(paise: int) -> str:
    return f"₹{paise // 100:,}.{paise % 100:02d}"


class CredentialsDialog(QDialog):
    """Asks for the credentials the BACKUP was made with (username + password, or recovery key)."""

    def __init__(self, username: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open backup")
        self.by_password = QRadioButton("Username and password (as they were on the backup date)")
        self.by_recovery = QRadioButton("Recovery key")
        self.by_password.setChecked(True)
        self.user = QLineEdit(username)
        self.password = PasswordField("Password")
        self.recovery = PasswordField("Recovery key")

        fields = form()
        fields.addRow("Username", self.user)
        fields.addRow("Password", self.password)
        fields.addRow("Recovery key", self.recovery)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        col = QVBoxLayout(self)
        col.addWidget(label("Type the credentials this backup was made with."))
        col.addWidget(self.by_password)
        col.addWidget(self.by_recovery)
        col.addLayout(fields)
        col.addWidget(buttons)

        self.by_recovery.toggled.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        recovery = self.by_recovery.isChecked()
        self.user.setEnabled(not recovery)
        self.password.setEnabled(not recovery)
        self.recovery.setEnabled(recovery)

    def values(self):
        if self.by_recovery.isChecked():
            return "recovery", self.recovery.text().strip()
        return self.user.text().strip(), self.password.text()


class BackupRestoreView(QWidget):
    """'Create Backup', 'Test this backup' and 'Restore Backup'."""

    restored = Signal()  # a restore finished; the database was closed, so sign in again

    COLUMNS = ["File", "Created", "Size", "Status"]

    def __init__(self, conn, session, folder=None, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.conn, self.session = conn, session
        self.folder = Path(folder) if folder else data_folder()
        self.dest = get_setting(conn, FOLDER_SETTING) or ""

        self.can_create = has_permission(conn, session.user_id, "create_backup")
        self.can_restore = has_permission(conn, session.user_id, "restore_backup")

        self.status = label("", "subtitle")
        self.result = label("")
        self.folder_edit = QLineEdit(self.dest)
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText("Choose where to save backups (a USB drive is best)")
        self.choose_btn = QPushButton("Choose folder...")
        self.create_btn = QPushButton("Create Backup")
        self.test_btn = QPushButton("Test this backup")
        self.restore_btn = QPushButton("Restore Backup")

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.choose_btn.clicked.connect(self._choose_folder_clicked)
        self.create_btn.clicked.connect(lambda: self.create_backup())
        self.test_btn.clicked.connect(lambda: self.test_backup())
        self.restore_btn.clicked.connect(lambda: self.restore_backup())

        self.create_btn.setEnabled(self.can_create)
        self.choose_btn.setEnabled(self.can_create or self.can_restore)
        self.test_btn.setEnabled(self.can_create or self.can_restore)
        self.restore_btn.setEnabled(self.can_restore)

        row = QHBoxLayout()
        row.addWidget(self.folder_edit, 1)
        row.addWidget(self.choose_btn)
        buttons = QHBoxLayout()
        buttons.addWidget(self.create_btn)
        buttons.addWidget(self.test_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.restore_btn)

        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label("Backup and Restore", "title"))
        col.addWidget(self.status)
        col.addLayout(row)
        col.addLayout(buttons)
        col.addWidget(self.result)
        col.addWidget(self.table, 1)

        self.refresh()

    # ------------------------------------------------------------ screen state

    def refresh(self) -> None:
        """Reload the reminder line and the list of backups in the chosen folder."""
        try:
            due = backup.backup_due(self.conn)
        except Exception:
            log.exception("E-BACKUP-STATUS: could not read the backup status")
            due = None
        if due is None:
            self.status.setText("")
        elif due["last_backup_at"] is None:
            self.status.setText("No backup has been made yet. Please create one now.")
        else:
            text = f"Last backup: {pretty_time(due['last_backup_at'])} ({due['days_since']} day(s) ago)."
            self.status.setText(text + ("  A new backup is due." if due["due"] else ""))

        self.table.setRowCount(0)
        if self.dest and Path(self.dest).is_dir():
            for item in backup.list_backups(self.dest):
                r = self.table.rowCount()
                self.table.insertRow(r)
                cells = [
                    item["file_name"],
                    pretty_time(item["created_at"]) if item["created_at"] else "-",
                    f"{item['size_bytes'] / 1024 / 1024:.1f} MB",
                    "OK" if item["intact"] else "DAMAGED",
                ]
                for c, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    if c == 0:
                        cell.setData(Qt.UserRole, item["path"])
                    self.table.setItem(r, c, cell)

    def set_destination(self, dest) -> None:
        self.dest = str(dest)
        self.folder_edit.setText(self.dest)
        try:
            set_setting(self.conn, FOLDER_SETTING, self.dest)
        except Exception:
            log.exception("E-BACKUP-FOLDER: could not remember the backup folder")
        self.refresh()

    # ------------------------------------------------------------ small dialogs (replaced in tests)

    def _choose_folder(self) -> str:
        start = self.dest or str(Path.home())
        return QFileDialog.getExistingDirectory(self, "Choose backup folder", start)

    def _choose_folder_clicked(self) -> None:
        chosen = self._choose_folder()
        if chosen:
            self.set_destination(chosen)

    def _pick_backup(self) -> str:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if rows:
            return self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a backup file", self.dest or str(Path.home()), f"Cloth Shop backup (*{backup.EXT})"
        )
        return path

    def _ask_credentials(self):
        dialog = CredentialsDialog(self.session.username, self)
        if dialog.exec() != QDialog.Accepted:
            return None
        values = dialog.values()
        dialog.password.clear()
        dialog.recovery.clear()
        return values

    def _info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    def _confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Yes | QMessageBox.No, self)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    # ------------------------------------------------------------ errors

    def _fail(self, exc: Exception, code: str) -> str:
        """Short reason for the owner. Technical detail goes to the local log only."""
        if isinstance(exc, (BackupError, PermissionDenied, SessionLocked, ValueError)):
            return str(exc)
        log.exception("%s: unexpected error", code)
        return f"Something went wrong (error {code}). Please try again or restart the app."

    def _database_still_open(self) -> bool:
        try:
            get_setting(self.conn, FOLDER_SETTING)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ Create Backup

    def create_backup(self, dest=None):
        """The 'Create Backup' button. Returns the result dict, or None if it failed or was cancelled."""
        dest = dest or self.dest
        if not dest:
            dest = self._choose_folder()
            if not dest:
                return None
        if str(dest) != self.dest:
            self.set_destination(dest)  # remember it and list the backups already there
        started = datetime.now().strftime(_FMT)
        try:
            result = backup.create_backup(self.conn, self.session, self.folder, dest)
        except Exception as exc:
            reason = self._fail(exc, "E-BACKUP")
            self.result.setText(f"Backup FAILED on {pretty_time(started)}.\n{reason}")
            self._error("Backup failed", reason)
            self.refresh()
            return None

        lines = [
            f"Backup SUCCESSFUL on {pretty_time(result['created_at'])}.",
            f"File: {result['path']}",
            f"Bills included: {result['bill_count']}",
        ]
        if result.get("same_disk_warning"):
            lines.append(
                "Warning: this backup is on the same disk as your shop data. "
                "If the laptop is lost or damaged, both are lost. Please also copy it to a USB drive."
            )
        self.result.setText("\n".join(lines))
        self.refresh()
        return result

    # ------------------------------------------------------------ Test this backup

    def test_backup(self, path=None, slot=None, secret=None):
        """'Test this backup': opens it read-only and shows record counts. Changes nothing."""
        path = path or self._pick_backup()
        if not path:
            return None
        if slot is None:
            creds = self._ask_credentials()
            if not creds:
                return None
            slot, secret = creds
        try:
            info = backup.inspect_backup(path, slot, secret)
        except Exception as exc:
            reason = self._fail(exc, "E-BACKUP-TEST")
            self.result.setText(f"Backup test FAILED.\n{reason}")
            self._error("Backup test failed", reason)
            return None
        self.result.setText("Backup test passed.\n" + self._preview_text(info))
        self._info("Backup test passed", self._preview_text(info))
        return info

    @staticmethod
    def _preview_text(info: dict) -> str:
        counts = info["counts"]
        return (
            f"Backup date: {pretty_time(info['created_at'])}\n"
            f"Bills: {info['bill_count']}   Last bill: {info['last_bill_no'] or '-'}\n"
            f"Sales total: {rupees(info['sales_total_paise'])}\n"
            f"Products: {counts.get('products', 0)}   Variants: {counts.get('variants', 0)}   "
            f"Customers: {counts.get('customers', 0)}   Suppliers: {counts.get('suppliers', 0)}"
        )

    # ------------------------------------------------------------ Restore Backup

    def restore_backup(self, path=None, slot=None, secret=None):
        """The 'Restore Backup' button. Preview first, then confirm. Returns the result or None."""
        path = path or self._pick_backup()
        if not path:
            return None
        if slot is None:
            creds = self._ask_credentials()
            if not creds:
                return None
            slot, secret = creds
        try:
            info = backup.inspect_backup(path, slot, secret)
        except Exception as exc:
            reason = self._fail(exc, "E-RESTORE-CHECK")
            self.result.setText(f"Restore stopped. Nothing was changed.\n{reason}")
            self._error("Cannot restore this backup", reason)
            return None

        question = (
            "Restoring will REPLACE all current shop data with this backup.\n\n"
            + self._preview_text(info)
            + "\n\nA safety backup of your current data is made first. "
              "You will need to sign in again, and passwords will be as they were on the backup date.\n\n"
              "Do you want to restore?"
        )
        if not self._confirm("Restore Backup", question):
            return None

        try:
            result = backup.restore_backup(
                self.folder, path, slot, secret, conn=self.conn, session=self.session, confirm=True
            )
        except Exception as exc:
            reason = self._fail(exc, "E-RESTORE")
            self.result.setText(f"Restore FAILED.\n{reason}")
            self._error("Restore failed", reason)
            if not self._database_still_open():
                self.restored.emit()  # the old data is closed; go back to sign-in
            return None

        self._info(
            "Restore complete",
            f"{result['message']}\n\nBills restored: {result['bill_count']}\n"
            f"Safety copy of the previous data: {result['safety_backup'] or '-'}",
        )
        self.restored.emit()
        return result
