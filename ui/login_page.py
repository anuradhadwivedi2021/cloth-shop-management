"""Sign-in, lock screen, and 'forgot password' (recovery key) screens."""

import logging

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core import auth, shop
from core.session import SessionLocked
from ui.widgets import PasswordField, centered, form, label, make_card

log = logging.getLogger("clothshop")


def friendly_error(exc: Exception, code: str) -> str:
    """Short reason for the screen; the technical detail goes to the local log only (no passwords)."""
    if isinstance(exc, auth.AccountLocked):
        return f"Too many wrong attempts. Please wait {exc.seconds} seconds and try again."
    if isinstance(exc, (auth.AuthError, ValueError, SessionLocked)):
        return str(exc)
    log.exception("%s: unexpected error", code)
    return f"Something went wrong (error {code}). Please try again or restart the app."


class _Countdown:
    """Disables a button and shows seconds left while an account is locked out."""

    def __init__(self, button, message_label):
        self.button, self.message = button, message_label
        self.left = 0
        self.timer = QTimer()
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

    def start(self, seconds: int) -> None:
        self.left = seconds
        self.button.setEnabled(False)
        self._show()
        self.timer.start()

    def _show(self) -> None:
        self.message.setText(f"Too many wrong attempts. Try again in {self.left} seconds.")

    def _tick(self) -> None:
        self.left -= 1
        if self.left <= 0:
            self.timer.stop()
            self.message.setText("")
            self.button.setEnabled(True)
        else:
            self._show()


class LoginPage(QWidget):
    signed_in = Signal(object, object)  # conn, session

    def __init__(self, folder, shop_name="", parent=None):
        super().__init__(parent)
        self.folder = folder
        card, col = make_card(440)
        self.title = label(shop_name or "Cloth Shop", "title")
        col.addWidget(self.title)
        col.addWidget(label("Please sign in", "subtitle"))
        lay = form()
        self.username = QLineEdit()
        self.password = PasswordField()
        lay.addRow("Username", self.username)
        lay.addRow("Password", self.password)
        col.addLayout(lay)
        self.error = label("", "error")
        col.addWidget(self.error)
        self.button = QPushButton("Sign in")
        self.button.setObjectName("primary")
        col.addWidget(self.button)
        self.forgot = QPushButton("Forgot password? Use recovery key")
        self.forgot.setObjectName("link")
        col.addWidget(self.forgot)
        self.countdown = _Countdown(self.button, self.error)
        self.button.clicked.connect(self._sign_in)
        self.password.returnPressed.connect(self._sign_in)
        self.username.returnPressed.connect(self.password.setFocus)
        self.forgot.clicked.connect(self._recover)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(centered(card))

    def reset(self, shop_name="") -> None:
        if shop_name:
            self.title.setText(shop_name)
        self.password.clear()
        self.error.setText("")
        (self.password if self.username.text() else self.username).setFocus()

    def _sign_in(self) -> None:
        if not self.button.isEnabled():
            return
        try:
            conn, session = shop.open_shop(self.folder, self.username.text().strip(), self.password.text())
        except auth.AccountLocked as exc:
            self.password.clear()
            self.countdown.start(exc.seconds)
            return
        except Exception as exc:
            self.password.clear()
            self.error.setText(friendly_error(exc, "E-LOGIN"))
            return
        self.password.clear()
        self.error.setText("")
        self.signed_in.emit(conn, session)

    def _recover(self) -> None:
        dialog = RecoverDialog(self.folder, self)
        if dialog.exec() == QDialog.Accepted and dialog.result_pair:
            self.signed_in.emit(*dialog.result_pair)


class RecoverDialog(QDialog):
    """Forgotten owner password: recovery key + new password. Shows the NEW recovery key afterwards."""

    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.result_pair = None
        self.setWindowTitle("Recover access")
        self.setMinimumWidth(480)
        col = QVBoxLayout(self)
        col.setContentsMargins(24, 20, 24, 20)
        col.setSpacing(12)
        self.heading = label("Enter your recovery key and choose a new owner password.")
        col.addWidget(self.heading)
        self.fields = QWidget()
        lay = form()
        self.fields.setLayout(lay)
        self.key = QLineEdit()
        self.key.setPlaceholderText("XXXX-XXXX-XXXX-XXXX-XXXX-XXXX")
        self.new1 = PasswordField("At least 8 characters")
        self.new2 = PasswordField("Type it again")
        lay.addRow("Recovery key", self.key)
        lay.addRow("New password", self.new1)
        lay.addRow("Repeat password", self.new2)
        col.addWidget(self.fields)
        self.new_key = label("", "recovery")
        self.new_key.hide()
        col.addWidget(self.new_key)
        self.saved = QCheckBox("I have written down or saved the new recovery key")
        self.saved.hide()
        col.addWidget(self.saved)
        self.error = label("", "error")
        col.addWidget(self.error)
        row = QHBoxLayout()
        self.cancel = QPushButton("Cancel")
        self.go = QPushButton("Recover")
        self.go.setObjectName("primary")
        self.cancel.clicked.connect(self.reject)
        self.go.clicked.connect(self._go)
        row.addStretch(1)
        row.addWidget(self.cancel)
        row.addWidget(self.go)
        col.addLayout(row)
        self.saved.toggled.connect(self.go.setEnabled)

    def _go(self) -> None:
        if self.result_pair is not None:  # second click = "Continue"
            self.accept()
            return
        try:
            if self.new1.text() != self.new2.text():
                raise ValueError("The two passwords are not the same.")
            conn, session, new_key = shop.recover_access(
                self.folder, self.key.text().strip().upper(), self.new1.text())
        except Exception as exc:
            self.error.setText(friendly_error(exc, "E-RECOVER"))
            return
        self.result_pair = (conn, session)
        self.error.setText("")
        self.fields.hide()
        self.cancel.hide()
        self.heading.setText("Access restored. Your OLD recovery key no longer works. Save this new one now:")
        self.new_key.setText(new_key)
        self.new_key.show()
        self.saved.show()
        self.go.setText("Continue")
        self.go.setEnabled(False)

    def reject(self) -> None:
        if self.result_pair is not None:  # key not yet saved: do not let the owner skip it by accident
            return
        super().reject()


class LockPage(QWidget):
    """Shown after idle time or when the user presses Lock. Needs the same user's password."""

    unlocked = Signal()
    sign_out = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn = self._session = None
        card, col = make_card(420)
        col.addWidget(label("Locked", "title"))
        self.who = label("", "subtitle")
        col.addWidget(self.who)
        self.password = PasswordField("Password")
        col.addWidget(self.password)
        self.error = label("", "error")
        col.addWidget(self.error)
        self.button = QPushButton("Unlock")
        self.button.setObjectName("primary")
        col.addWidget(self.button)
        other = QPushButton("Sign out")
        other.setObjectName("link")
        col.addWidget(other)
        self.countdown = _Countdown(self.button, self.error)
        self.button.clicked.connect(self._unlock)
        self.password.returnPressed.connect(self._unlock)
        other.clicked.connect(self.sign_out)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(centered(card))

    def show_for(self, conn, session) -> None:
        self._conn, self._session = conn, session
        self.who.setText(f"Signed in as {session.username}. Enter your password to continue.")
        self.password.clear()
        self.error.setText("")
        self.password.setFocus()

    def _unlock(self) -> None:
        if not self.button.isEnabled():
            return
        try:
            self._session.unlock(self._conn, self.password.text())
        except auth.AccountLocked as exc:
            self.password.clear()
            self.countdown.start(exc.seconds)
            return
        except auth.AuthError:
            self.password.clear()
            self.error.setText("Wrong password.")
            return
        except Exception as exc:
            self.password.clear()
            self.error.setText(friendly_error(exc, "E-UNLOCK"))
            return
        self.password.clear()
        self.unlocked.emit()
