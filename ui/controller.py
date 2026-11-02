"""Decides which screen is showing: setup -> sign in -> main -> lock, and runs the idle auto-lock."""

import logging

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QStackedWidget

from core import shop
from core.session import SessionLocked
from core.shop_settings import get_shop_profile
from ui.login_page import LockPage, LoginPage
from ui.main_window import MainWindow
from ui.setup_wizard import SetupPage

log = logging.getLogger("clothshop")

_ACTIVITY = (QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Wheel)


class AppController(QStackedWidget):
    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.conn = self.session = self.main = None
        self.setWindowTitle("Cloth Shop Management")
        self.resize(1180, 760)

        self.setup_page = SetupPage(folder)
        self.login_page = LoginPage(folder)
        self.lock_page = LockPage()
        for page in (self.setup_page, self.login_page, self.lock_page):
            self.addWidget(page)

        self.setup_page.done.connect(self._signed_in)
        self.login_page.signed_in.connect(self._signed_in)
        self.lock_page.unlocked.connect(self._unlocked)
        self.lock_page.sign_out.connect(self.sign_out)

        self._watch = _ActivityWatcher(self)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._check_idle)
        self._timer.start()

        self.setCurrentWidget(self.login_page if shop.shop_exists(folder) else self.setup_page)

    # ---------- which screen ----------

    def current_name(self) -> str:
        page = self.currentWidget()
        for name in ("setup_page", "login_page", "lock_page", "main"):
            if page is getattr(self, name, None):
                return name.replace("_page", "")
        return "?"

    def _signed_in(self, conn, session) -> None:
        self._drop_main()
        self.conn, self.session = conn, session
        self.main = MainWindow(conn, session, get_shop_profile(conn))
        self.main.lock_requested.connect(self.lock)
        self.main.sign_out_requested.connect(self.sign_out)
        self.addWidget(self.main)
        self.setCurrentWidget(self.main)

    def _drop_main(self) -> None:
        if self.main is not None:
            self.removeWidget(self.main)
            self.main.deleteLater()
            self.main = None

    # ---------- lock / sign out ----------

    def lock(self) -> None:
        if self.session is None or self.currentWidget() is self.lock_page:
            return
        self.session.lock()
        self.lock_page.show_for(self.conn, self.session)
        self.setCurrentWidget(self.lock_page)

    def _unlocked(self) -> None:
        self.setCurrentWidget(self.main)

    def sign_out(self) -> None:
        name = (get_shop_profile(self.conn)["name"] or "") if self.conn else ""
        self._drop_main()
        self.close_database()
        self.login_page.reset(name)
        self.setCurrentWidget(self.login_page)

    def close_database(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                log.exception("E-CLOSE: could not close the database cleanly")
        self.conn = self.session = None

    def _check_idle(self) -> None:
        if self.session is not None and self.currentWidget() is self.main and self.session.locked:
            self.lock()

    def note_activity(self) -> None:
        if self.session is not None and self.currentWidget() is self.main:
            try:
                self.session.touch()
            except SessionLocked:
                self.lock()

    def closeEvent(self, event) -> None:
        self.close_database()
        super().closeEvent(event)


class _ActivityWatcher(QObject):
    """Any click, key press or scroll counts as 'the user is still here' for the auto-lock."""

    def __init__(self, controller: AppController):
        super().__init__(controller)
        self.controller = controller
        from PySide6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in _ACTIVITY:
            self.controller.note_activity()
        return False
