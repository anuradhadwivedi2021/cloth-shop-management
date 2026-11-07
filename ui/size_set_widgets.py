"""Size sets on screen (PRD-04): a picker that fills a sizes box, and a small manager window.

No rules here: core.size_sets reads, checks, saves and audits. The picker only copies the chosen
set's sizes into the box next to it, where the owner can still edit them before saving.
"""

import logging

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QListWidget, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from core import size_sets
from core.permissions import PermissionDenied, has_permission
from ui.i18n import tr
from ui.widgets import form, label

log = logging.getLogger(__name__)


def _show_error(parent, exc):
    if isinstance(exc, (ValueError, PermissionDenied)):
        QMessageBox.warning(parent, tr("Size sets"), tr(str(exc)))
    else:
        log.exception("Size set action failed")
        QMessageBox.warning(parent, tr("Size sets"), tr("Something went wrong. Nothing was changed."))


class ManageSizeSetsDialog(QDialog):
    """List, add, change and delete size sets. Existing products are never touched."""

    def __init__(self, conn, user_id, parent=None):
        super().__init__(parent)
        self.conn, self.user_id = conn, user_id
        self.setWindowTitle(tr("Size sets"))
        self.setMinimumWidth(460)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentRowChanged.connect(self._selected)
        self.name_edit, self.sizes_edit = QLineEdit(), QLineEdit()
        self.sizes_edit.setPlaceholderText(tr("S, M, L, XL"))
        rows = form()
        rows.addRow(tr("Name"), self.name_edit)
        rows.addRow(tr("Sizes (comma separated)"), self.sizes_edit)
        self.add_btn = QPushButton(tr("Add as new set"))
        self.save_btn = QPushButton(tr("Save changes to selected"))
        self.delete_btn = QPushButton(tr("Delete selected"))
        self.add_btn.clicked.connect(self._add)
        self.save_btn.clicked.connect(self._save)
        self.delete_btn.clicked.connect(self._delete)
        buttons = QHBoxLayout()
        for button in (self.add_btn, self.save_btn, self.delete_btn):
            buttons.addWidget(button)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        col = QVBoxLayout(self)
        col.addWidget(self.list)
        col.addLayout(rows)
        col.addLayout(buttons)
        col.addWidget(label(tr("Changing or deleting a size set does not change products you already made."), "hint"))
        col.addWidget(close)
        allowed = has_permission(conn, user_id, "edit_products")
        for widget in (self.add_btn, self.save_btn, self.delete_btn, self.name_edit, self.sizes_edit):
            widget.setEnabled(allowed)
        self._reload()

    def _reload(self, select=None):
        self._sets = size_sets.list_size_sets(self.conn)
        self.list.blockSignals(True)
        self.list.clear()
        for item in self._sets:
            self.list.addItem(f"{item['name']}: {', '.join(item['sizes'])}")
        self.list.blockSignals(False)
        names = [s["name"].lower() for s in self._sets]
        row = names.index(select.lower()) if select and select.lower() in names else -1
        self.list.setCurrentRow(row)
        if row < 0:
            self.name_edit.clear()
            self.sizes_edit.clear()

    def _selected(self, row):
        if 0 <= row < len(self._sets):
            self.name_edit.setText(self._sets[row]["name"])
            self.sizes_edit.setText(", ".join(self._sets[row]["sizes"]))

    def _current_name(self):
        row = self.list.currentRow()
        return self._sets[row]["name"] if 0 <= row < len(self._sets) else None

    def _run(self, action, select):
        try:
            action()
        except Exception as exc:
            _show_error(self, exc)
            return
        self._reload(select)

    def _add(self):
        name = self.name_edit.text()
        self._run(lambda: size_sets.save_size_set(self.conn, self.user_id, name, self.sizes_edit.text()), name)

    def _save(self):
        old = self._current_name()
        if old is None:
            QMessageBox.warning(self, tr("Size sets"), tr("First select a size set in the list."))
            return
        name = self.name_edit.text()
        self._run(lambda: size_sets.save_size_set(
            self.conn, self.user_id, name, self.sizes_edit.text(), replaces=old), name)

    def _delete(self):
        old = self._current_name()
        if old is None:
            QMessageBox.warning(self, tr("Size sets"), tr("First select a size set in the list."))
            return
        sure = QMessageBox.question(self, tr("Size sets"), tr("Delete the size set {name}?", name=old))
        if sure == QMessageBox.Yes:
            self._run(lambda: size_sets.delete_size_set(self.conn, self.user_id, old), None)


class SizeSetPicker(QWidget):
    """'Use a size set' drop-down that fills `target` (a QLineEdit), plus a Manage button."""

    def __init__(self, conn, user_id, target: QLineEdit, parent=None):
        super().__init__(parent)
        self.conn, self.user_id, self.target = conn, user_id, target
        self.combo = QComboBox()
        self.manage_btn = QPushButton(tr("Manage size sets"))
        self.combo.activated.connect(self._chosen)
        self.manage_btn.clicked.connect(self._manage)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.combo, 1)
        row.addWidget(self.manage_btn)
        self.reload()

    def reload(self):
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(tr("Use a size set…"), "")
        for item in size_sets.list_size_sets(self.conn):
            self.combo.addItem(f"{item['name']} ({', '.join(item['sizes'])})", item["name"])
        self.combo.blockSignals(False)

    def _chosen(self, index):
        name = self.combo.itemData(index)
        if name:
            self.target.setText(size_sets.sizes_text(self.conn, name))
        self.combo.setCurrentIndex(0)

    def _manage(self):
        ManageSizeSetsDialog(self.conn, self.user_id, self).exec()
        self.reload()
