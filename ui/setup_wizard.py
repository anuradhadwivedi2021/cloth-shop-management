"""First-time setup: shop details, tax mode, owner account, and the one-time recovery key."""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QPushButton, QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)

from core import auth, shop
from core.customers import validate_gstin
from core.shop_settings import update_preferences, update_shop_profile
from ui.widgets import PasswordField, centered, form, label, make_card

log = logging.getLogger("clothshop")


def check_shop_details(name, phone, tax_mode, gstin):
    """Catch mistakes on the screen before anything is created. Returns the cleaned GSTIN ('' if none)."""
    if len((name or "").strip()) < 2:
        raise ValueError("Please enter the shop name.")
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if (phone or "").strip() and not 7 <= len(digits) <= 15:
        raise ValueError("Phone number should have 7 to 15 digits.")
    if tax_mode == "unregistered":
        return ""
    if not (gstin or "").strip():
        raise ValueError("A GST-registered shop needs its GSTIN. If the shop has no GSTIN, choose 'Not registered'.")
    return validate_gstin(gstin)

TAX_MODES = {
    "regular": "GST registered (tax invoice)",
    "composition": "Composition scheme (bill of supply, no tax shown)",
    "unregistered": "Not registered for GST (simple bill)",
}
AUTOLOCK_CHOICES = [1, 2, 3, 5, 10, 15, 30, 60]
STEP_TITLES = ["Your shop", "Tax", "Owner account", "Recovery key"]


class SetupPage(QWidget):
    """Emits `done(conn, session)` when the owner has saved the recovery key and pressed Finish."""

    done = Signal(object, object)
    use_counter = Signal()          # this PC is a second counter, not a new shop

    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = folder
        self._conn = None
        self._session = None
        self._profile = None

        card, col = make_card(620)
        col.addWidget(label("Welcome! Let's set up your shop", "title"))
        self.step_label = label("", "subtitle")
        col.addWidget(self.step_label)
        self.stack = QStackedWidget()
        for build in (self._shop_step, self._tax_step, self._owner_step, self._key_step):
            self.stack.addWidget(build())
        col.addWidget(self.stack)
        self.error = label("", "error")
        self.error.setAccessibleName("setup error")
        col.addWidget(self.error)

        buttons = QHBoxLayout()
        self.back_btn = QPushButton("Back")
        self.next_btn = QPushButton("Next")
        self.next_btn.setObjectName("primary")
        self.back_btn.clicked.connect(self._back)
        self.next_btn.clicked.connect(self._next)
        buttons.addWidget(self.back_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.next_btn)
        col.addLayout(buttons)
        self.counter_btn = QPushButton("This is a second counter: connect to the main counter")
        self.counter_btn.setObjectName("link")
        self.counter_btn.clicked.connect(self.use_counter.emit)
        col.addWidget(self.counter_btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(centered(card))
        self._show_step(0)

    # ---------- steps ----------

    def _shop_step(self):
        page, lay = QWidget(), form()
        page.setLayout(lay)
        self.shop_name = QLineEdit()
        self.shop_name.setPlaceholderText("e.g. Anu Fashions")
        self.shop_address = QPlainTextEdit()
        self.shop_address.setFixedHeight(80)
        self.shop_phone = QLineEdit()
        self.shop_phone.setPlaceholderText("Shown on bills")
        lay.addRow("Shop name *", self.shop_name)
        lay.addRow("Address", self.shop_address)
        lay.addRow("Phone", self.shop_phone)
        return page

    def _tax_step(self):
        page, col = QWidget(), QVBoxLayout()
        page.setLayout(col)
        col.addWidget(label("How is your shop registered for GST?"))
        self.tax_group = QButtonGroup(self)
        self.tax_buttons = {}
        for key, text in TAX_MODES.items():
            button = QRadioButton(text)
            self.tax_group.addButton(button)
            self.tax_buttons[key] = button
            col.addWidget(button)
        self.tax_buttons["unregistered"].setChecked(True)
        lay = form()
        self.gstin = QLineEdit()
        self.gstin.setPlaceholderText("15-character GSTIN")
        lay.addRow("GSTIN", self.gstin)
        col.addLayout(lay)
        self.autolock = QComboBox()
        for minutes in AUTOLOCK_CHOICES:
            self.autolock.addItem(f"{minutes} minute{'s' if minutes > 1 else ''}", minutes)
        self.autolock.setCurrentIndex(AUTOLOCK_CHOICES.index(5))
        lay2 = form()
        lay2.addRow("Lock the app after being idle for", self.autolock)
        col.addLayout(lay2)
        col.addWidget(label(
            "Please confirm this with your accountant. It decides the bill format and can be changed later "
            "in Settings. Old bills never change.", "hint"))
        for button in self.tax_buttons.values():
            button.toggled.connect(self._tax_changed)
        self._tax_changed()
        return page

    def _owner_step(self):
        page, lay = QWidget(), form()
        page.setLayout(lay)
        self.full_name = QLineEdit()
        self.username = QLineEdit()
        self.username.setPlaceholderText("Used to sign in, e.g. owner")
        self.password = PasswordField("At least 8 characters")
        self.password2 = PasswordField("Type it again")
        self.pin = PasswordField("4 to 6 digits")
        lay.addRow("Your name", self.full_name)
        lay.addRow("Username *", self.username)
        lay.addRow("Password *", self.password)
        lay.addRow("Repeat password *", self.password2)
        lay.addRow("Owner PIN *", self.pin)
        lay.addRow(label(
            "The PIN lets you approve big discounts and special returns at the counter without sharing your "
            "password.", "hint"))
        return page

    def _key_step(self):
        page, col = QWidget(), QVBoxLayout()
        page.setLayout(col)
        col.addWidget(label(
            "Your shop is created. Below is your recovery key. It is shown only once.", None))
        self.key_label = label("", "recovery")
        self.key_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.key_label)
        col.addWidget(label(
            "If you ever forget your password, this key is the only way back into your data. Nobody else, "
            "including the developers, can recover it. Write it on paper and keep it away from this laptop.",
            "hint"))
        row = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.key_label.text()))
        save = QPushButton("Save to a file (USB drive)")
        save.clicked.connect(self._save_key)
        row.addWidget(copy)
        row.addWidget(save)
        row.addStretch(1)
        col.addLayout(row)
        self.saved_box = QCheckBox("I have written down or saved my recovery key")
        self.saved_box.toggled.connect(lambda on: self.next_btn.setEnabled(on))
        col.addWidget(self.saved_box)
        return page

    # ---------- behaviour ----------

    def _tax_changed(self, *_):
        registered = not self.tax_buttons["unregistered"].isChecked()
        self.gstin.setEnabled(registered)
        if not registered:
            self.gstin.clear()

    def _tax_mode(self) -> str:
        return next(key for key, button in self.tax_buttons.items() if button.isChecked())

    def _show_step(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.step_label.setText(f"Step {index + 1} of 4: {STEP_TITLES[index]}")
        self.error.setText("")
        self.back_btn.setVisible(0 < index < 3)
        self.next_btn.setText({2: "Create my shop", 3: "Finish"}.get(index, "Next"))
        self.next_btn.setEnabled(index != 3 or self.saved_box.isChecked())

    def _fail(self, message: str) -> None:
        self.error.setText(message)

    def _back(self) -> None:
        self._show_step(self.stack.currentIndex() - 1)

    def _next(self) -> None:
        step = self.stack.currentIndex()
        try:
            if step == 0:
                if len(self.shop_name.text().strip()) < 2:
                    raise ValueError("Please enter the shop name.")
                self._show_step(1)
            elif step == 1:
                gstin = check_shop_details(
                    self.shop_name.text(), self.shop_phone.text(), self._tax_mode(), self.gstin.text())
                self._profile = {
                    "name": self.shop_name.text().strip(), "address": self.shop_address.toPlainText().strip(),
                    "phone": self.shop_phone.text().strip(), "gstin": gstin, "tax_scheme": self._tax_mode(),
                }
                self._show_step(2)
            elif step == 2:
                self._create_shop()
            else:
                self._finish()
        except ValueError as exc:
            self._fail(str(exc))

    def _create_shop(self) -> None:
        username = self.username.text().strip()
        if self.password.text() != self.password2.text():
            raise ValueError("The two passwords are not the same.")
        auth.validate_password(self.password.text())
        auth.validate_pin(self.pin.text())
        try:
            conn, session, recovery = shop.setup_new_shop(
                self.folder, username, self.password.text(),
                pin=self.pin.text(), full_name=self.full_name.text().strip() or None,
            )
            try:
                update_shop_profile(conn, session.user_id, **self._profile)
                update_preferences(conn, session.user_id, {"auto_lock_minutes": self.autolock.currentData()})
                session.set_timeout_minutes(self.autolock.currentData())
            except Exception:
                conn.close()  # leave nothing half-made behind, so the owner can simply try again
                for name in (shop.DB_FILE, shop.KEY_FILE):
                    (Path(self.folder) / name).unlink(missing_ok=True)
                raise
        except ValueError:
            raise
        except Exception:
            log.exception("E-SETUP: could not create the shop")
            raise ValueError("Could not create the shop (error E-SETUP). Check that the disk has free space and try again.")
        self._conn, self._session = conn, session
        self.password.clear()
        self.password2.clear()
        self.pin.clear()
        self.key_label.setText(recovery)
        self._show_step(3)

    def _save_key(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save recovery key", "ClothShop-recovery-key.txt", "Text (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(f"Cloth Shop recovery key for {self._profile['name']}\n{self.key_label.text()}\n")
            except OSError:
                self._fail("Could not save the file. Choose another place, or write the key on paper.")

    def _finish(self) -> None:
        conn, session = self._conn, self._session
        self._conn = self._session = None
        self.key_label.setText("")
        self.done.emit(conn, session)
