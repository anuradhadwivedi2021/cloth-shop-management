"""The main screen after sign-in: top bar, menu (only what this user may open) and the work area.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from core.permissions import has_permission
from core.session import SessionLocked
from ui.widgets import label

# Import Real Functional Views
from ui.billing_counter_view import FastBillingCounterView
from ui.purchases_view import PurchasesManagementView
from ui.returns_customer_view import ReturnsAndCustomerLedgerView
from ui.cash_audit_view import CashClosingAndAuditView
from ui.dashboard_reports_view import DashboardAndReportsView
from ui.products_matrix_view import MatrixProductDialog
from ui.users_permissions_view import UsersAndPermissionsView
from ui.settings_view import SettingsView
from ui.expenses_view import ExpensesView
from ui.stock_count_view import StockCountView
from ui.suppliers_view import SuppliersManagementView
from ui.customers_view import CustomersView
from ui.purchase_returns_view import PurchaseReturnsView

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
        self.profile = profile
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
        """Returns the real functional view widget for each menu option."""
        user_id = self.session.user_id

        if text in ["Dashboard", "Reports", "Backup and Restore"]:
            return DashboardAndReportsView(db_connection=self.conn, user_id=user_id)

        elif text == "New Sale / Billing":
            return FastBillingCounterView(db_connection=self.conn, user_id=user_id)

        elif text == "Purchases":
            return PurchasesManagementView(db_connection=self.conn, user_id=user_id)

        elif text == "Suppliers":
            return SuppliersManagementView(db_connection=self.conn, user_id=user_id)

        elif text == "Customers":
            return CustomersView(db_connection=self.conn, user_id=user_id)

        elif text == "Sales Returns and Exchanges":
            return ReturnsAndCustomerLedgerView(db_connection=self.conn, user_id=user_id)

        elif text == "Purchase Returns":
            return PurchaseReturnsView(db_connection=self.conn, user_id=user_id)

        elif text == "Payments and Cash":
            return CashClosingAndAuditView(db_connection=self.conn, user_id=user_id)

        elif text == "Expenses":
            return ExpensesView(db_connection=self.conn, user_id=user_id)

        elif text == "Stock Count and Adjustments":
            return StockCountView(db_connection=self.conn, user_id=user_id)

        elif text == "Users and Permissions":
            return UsersAndPermissionsView(db_connection=self.conn, user_id=user_id)

        elif text == "Settings":
            return SettingsView(db_connection=self.conn, user_id=user_id)

        elif text in ["Products", "Stock and Variants"]:
            return DashboardAndReportsView(db_connection=self.conn, user_id=user_id)

        return DashboardAndReportsView(db_connection=self.conn, user_id=user_id)

    def _open(self, row: int) -> None:
        if row >= 0:
            try:
                self.session.touch()
            except SessionLocked:
                self.lock_requested.emit()
                return
            
            selected_text = self.allowed[row]
            
            if selected_text == "Products":
                dlg = MatrixProductDialog(self, db_connection=self.conn, user_id=self.session.user_id)
                dlg.exec()
            
            self.pages.setCurrentIndex(row)