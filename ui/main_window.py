"""The main screen after sign-in: top bar, menu (only what this user may open) and the work area.

Each menu entry opens a page. Real pages are added step by step; until then a page says it is coming.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from core.permissions import has_permission
from core.session import SessionLocked
from ui.widgets import label

# (menu text, permissions that open it: any one is enough; None = everyone). Order follows SRS 4.2.
MENU = [
    ("Dashboard", None),
    ("New Sale / Billing", ("sell",)),
    ("Products", ("view_products",)),
    ("Stock and Variants", ("view_products",)),
    ("Purchases", ("receive_purchase",)),
    ("Suppliers", ("receive_purchase", "view_reports")),
    ("Customers", ("view_customers",)),
    ("Sales Returns and Exchanges", ("process_return",)),
    ("Purchase Returns", ("return_to_supplier",)),
    ("Stock Count and Adjustments", ("adjust_stock",)),
    ("Payments and Cash", ("manage_cash",)),
    ("Expenses", ("manage_expenses",)),
    ("Reports", ("view_reports",)),
    ("Users and Permissions", ("manage_users",)),
    ("Backup and Restore", ("create_backup", "restore_backup")),
    ("Settings", ("manage_settings",)),
]

ROLE_NAMES = {"owner": "Owner", "cashier": "Cashier", "stock": "Stock staff", "accountant": "Accountant"}


class PlaceholderPage(QWidget):
    def __init__(self, title: str, note: str):
        super().__init__()
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(title, "title"))
        col.addWidget(label(note, "subtitle"))
        col.addStretch(1)


class MainWindow(QWidget):
    lock_requested = Signal()
    sign_out_requested = Signal()

    def __init__(self, conn, session, profile: dict, parent=None):
        super().__init__(parent)
        self.conn, self.session = conn, session
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)

        top = QFrame()
        top.setObjectName("topbar")
        bar = QHBoxLayout(top)
        bar.setContentsMargins(18, 10, 18, 10)
        bar.addWidget(label(profile.get("name") or "Cloth Shop", None, wrap=False))
        bar.addStretch(1)
        who = session.username
        bar.addWidget(label(f"{who}  ({ROLE_NAMES.get(session.role, session.role)})", None, wrap=False))
        self.lock_btn = QPushButton("Lock  (Ctrl+L)")
        self.out_btn = QPushButton("Sign out")
        self.lock_btn.clicked.connect(self.lock_requested)
        self.out_btn.clicked.connect(self.sign_out_requested)
        bar.addWidget(self.lock_btn)
        bar.addWidget(self.out_btn)

        self.menu = QListWidget()
        self.menu.setObjectName("menu")
        self.menu.setFixedWidth(270)
        self.menu.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pages = QStackedWidget()
        self.allowed = []
        for text, needs in MENU:
            if needs is None or any(has_permission(conn, session.user_id, perm) for perm in needs):
                self.allowed.append(text)
                self.menu.addItem(QListWidgetItem(text))
                self.pages.addWidget(self._page_for(text, profile))
        self.menu.currentRowChanged.connect(self._open)
        self.menu.setCurrentRow(0)

        body = QHBoxLayout()
        body.setSpacing(0)
        body.addWidget(self.menu)
        body.addWidget(self.pages, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(top)
        outer.addLayout(body, 1)

        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.lock_requested.emit)

    def _page_for(self, text: str, profile: dict) -> QWidget:
        if text == "Dashboard":
            page = PlaceholderPage(
                f"Welcome to {profile.get('shop_name') or 'your shop'}",
                "Your shop is ready. Today's sales, low stock and cash will appear here in a later step.")
            return page
        return PlaceholderPage(text, "This screen will be built in an upcoming step.")

    def _open(self, row: int) -> None:
        if row >= 0:
            try:
                self.session.touch()
            except SessionLocked:
                self.lock_requested.emit()  # idle time ran out between the click and now
                return
            self.pages.setCurrentIndex(row)
