"""The main screen after sign-in: top bar, menu (only what this user may open) and the work area.

How pages are built (important):
  * Every menu entry gets a page. A page that fails to build shows a short error page instead of
    stopping the whole app, so one broken screen can never lock the owner out of the others.
  * Screens are imported only when they are built, so a problem inside one screen's file cannot
    stop the main window from opening.
  * Only screens that run on the tested core/ functions are connected. The others show a clear
    "not connected yet" page. They are connected one by one, each with its own tests, because a
    screen that does its own SQL skips permission checks, the audit log and the stock rules, and a
    billing screen must never say "saved" unless the bill really was saved.
"""

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.permissions import has_permission
from core.session import SessionLocked
from ui import screens
from ui.i18n import N_, load_language_from_settings, tr
from ui.page_stack import CurrentPageStack
from ui.widgets import label

log = logging.getLogger(__name__)

# (menu text, permissions that open it: any one is enough; None = everyone). Order follows SRS 4.2.
MENU = [
    (N_("Dashboard"), None),
    (N_("New Sale / Billing"), ("sell",)),
    (N_("Products"), ("view_products",)),
    (N_("Stock and Variants"), ("view_products",)),
    (N_("Purchases"), ("receive_purchase",)),
    (N_("Suppliers"), ("receive_purchase", "view_reports")),
    (N_("Customers"), ("view_customers",)),
    (N_("Sales Returns and Exchanges"), ("process_return",)),
    (N_("Purchase Returns"), ("return_to_supplier",)),
    (N_("Stock Count and Adjustments"), ("adjust_stock",)),
    (N_("Payments and Cash"), ("manage_cash",)),
    (N_("Expenses"), ("manage_expenses",)),
    (N_("Reports"), ("view_reports",)),
    (N_("Users and Permissions"), ("manage_users",)),
    (N_("Backup and Restore"), ("create_backup", "restore_backup")),
    (N_("Settings"), ("manage_settings",)),
]

ROLE_NAMES = {"owner": N_("Owner"), "cashier": N_("Cashier"), "stock": N_("Stock staff"), "accountant": N_("Accountant")}

# Screens not connected to the tested core/ functions yet: what the page tells the user.
PENDING = {}


class PlaceholderPage(QWidget):
    def __init__(self, title: str, note: str, name: str = "subtitle"):
        super().__init__()
        self.is_error = name == "error"
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(title, "title"))
        col.addWidget(label(note, name))
        col.addStretch(1)


class MainWindow(QWidget):
    lock_requested = Signal()
    sign_out_requested = Signal()
    restored = Signal()  # a backup was restored; the database is closed, go back to sign-in

    def __init__(self, conn, session, profile: dict, parent=None, folder=None):
        super().__init__(parent)
        load_language_from_settings(conn)  # the shop's language (English if not set)
        self.conn, self.session = conn, session
        self.folder = folder
        self.profile = profile
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)

        top = QFrame()
        top.setObjectName("topbar")
        bar = QHBoxLayout(top)
        bar.setContentsMargins(18, 10, 18, 10)
        bar.addWidget(label(profile.get("name") or tr("Cloth Shop"), None, wrap=False))
        bar.addStretch(1)
        who = session.username
        bar.addWidget(label(f"{who}  ({tr(ROLE_NAMES.get(session.role, session.role))})", None, wrap=False))
        self.lock_btn = QPushButton(tr("Lock  (Ctrl+L)"))
        self.out_btn = QPushButton(tr("Sign out"))
        self.lock_btn.clicked.connect(self.lock_requested)
        self.out_btn.clicked.connect(self.sign_out_requested)
        bar.addWidget(self.lock_btn)
        bar.addWidget(self.out_btn)

        self.menu = QListWidget()
        self.menu.setObjectName("menu")
        self.menu.setFixedWidth(270)
        self.menu.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pages = CurrentPageStack()
        self.allowed = []
        for text, needs in MENU:
            if needs is None or any(has_permission(conn, session.user_id, perm) for perm in needs):
                self.allowed.append(text)
                self.menu.addItem(QListWidgetItem(tr(text)))
                self.pages.addWidget(self._page_for(text))
        self.menu.currentRowChanged.connect(self._open)
        self.menu.setCurrentRow(0)

        body = QHBoxLayout()
        body.setSpacing(0)
        body.addWidget(self.menu)
        scroll = QScrollArea()  # a page taller than the window scrolls; the window never outgrows the screen
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.pages)
        body.addWidget(scroll, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(top)
        outer.addLayout(body, 1)

        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.lock_requested.emit)

    # ------------------------------------------------------------ pages

    def _page_for(self, text: str) -> QWidget:
        """The page for a menu entry. A failure becomes an error page, never a crash."""
        try:
            return self._build(text)
        except Exception:
            log.exception("Screen '%s' could not be built", text)
            return PlaceholderPage(
                tr(text), tr("This screen could not be opened. The rest of the app still works. "
                             "Please tell whoever looks after the software."), "error",
            )

    def _build(self, text: str) -> QWidget:
        conn, session, user_id = self.conn, self.session, self.session.user_id

        # A file in ui/screens/ for this menu entry always wins (see ui/screens/__init__.py).
        plugged = screens.build_screen(text, conn, session, self.folder)
        if plugged is not None:
            return plugged

        if text == "Dashboard":
            from ui.dashboard_page import DashboardPage
            return DashboardPage(conn, session)

        if text == "New Sale / Billing":
            from ui.billing_counter_view import BillingCounterView
            return BillingCounterView(conn, session, self.folder)

        if text == "Stock Count and Adjustments":
            from ui.stock_count_view import StockCountView
            return StockCountView(conn, session)

        if text == "Sales Returns and Exchanges":
            from ui.sales_returns_view import SalesReturnsView
            return SalesReturnsView(conn, session)

        if text == "Purchase Returns":
            from ui.purchase_returns_view import PurchaseReturnsView
            return PurchaseReturnsView(conn, session)

        if text == "Purchases":
            from ui.purchases_view import PurchasesView
            return PurchasesView(conn, session)

        if text == "Backup and Restore":
            from ui.backup_view import BackupRestoreView
            view = BackupRestoreView(conn, session, self.folder)
            view.restored.connect(self.restored)
            return view

        if text == "Suppliers":
            from ui.suppliers_view import SuppliersManagementView
            return SuppliersManagementView(db_connection=conn, user_id=user_id)

        if text == "Users and Permissions":
            from ui.users_permissions_view import UsersAndPermissionsView
            return UsersAndPermissionsView(db_connection=conn, user_id=user_id)

        return PlaceholderPage(tr(text), tr(PENDING.get(text, N_("This screen is not connected yet."))))

    def _open(self, row: int) -> None:
        if row < 0:
            return
        try:
            self.session.touch()
        except SessionLocked:
            self.lock_requested.emit()
            return
        page = self.pages.widget(row)
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                log.exception("Screen '%s' could not refresh", self.allowed[row])
        self.pages.setCurrentIndex(row)