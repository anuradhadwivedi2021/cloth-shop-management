# ui/users_permissions_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QComboBox
)
from PySide6.QtCore import Qt

class UsersAndPermissionsView(QWidget):
    """Users, Roles (Owner, Cashier, Stock, Accountant) and PIN Management View."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        
        self.setup_ui()
        self.load_users()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        
        # Left Panel: User List Table
        left_group = QGroupBox("Existing System Users & Roles")
        lg_layout = QVBoxLayout()
        
        self.table_users = QTableWidget(0, 4)
        self.table_users.setHorizontalHeaderLabels(["User ID", "Username", "Role", "Status"])
        lg_layout.addWidget(self.table_users)
        
        left_group.setLayout(lg_layout)
        layout.addWidget(left_group, stretch=6)
        
        # Right Panel: Add New User Form
        right_group = QGroupBox("Create / Reset User Account")
        rg_layout = QVBoxLayout()
        
        self.txt_username = QLineEdit()
        self.txt_username.setPlaceholderText("Enter Username...")
        
        self.txt_password = QLineEdit()
        self.txt_password.setPlaceholderText("Enter Password...")
        self.txt_password.setEchoMode(QLineEdit.Password)
        
        self.cmb_role = QComboBox()
        self.cmb_role.addItems(["Cashier", "Stock staff", "Accountant", "Owner"])
        
        self.txt_pin = QLineEdit()
        self.txt_pin.setPlaceholderText("4-Digit Override PIN (Optional)")
        self.txt_pin.setMaxLength(4)
        
        btn_create = QPushButton("Create / Update User")
        btn_create.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 10px;")
        btn_create.clicked.connect(self.save_user)
        
        rg_layout.addWidget(QLabel("Username:"))
        rg_layout.addWidget(self.txt_username)
        rg_layout.addWidget(QLabel("Password:"))
        rg_layout.addWidget(self.txt_password)
        rg_layout.addWidget(QLabel("Role:"))
        rg_layout.addWidget(self.cmb_role)
        rg_layout.addWidget(QLabel("Owner Override PIN:"))
        rg_layout.addWidget(self.txt_pin)
        rg_layout.addWidget(btn_create)
        rg_layout.addStretch()
        
        right_group.setLayout(rg_layout)
        layout.addWidget(right_group, stretch=4)

    def load_users(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, username, role, is_active FROM users")
        rows = cursor.fetchall()
        
        self.table_users.setRowCount(0)
        for user_id, uname, role, active in rows:
            idx = self.table_users.rowCount()
            self.table_users.insertRow(idx)
            self.table_users.setItem(idx, 0, QTableWidgetItem(str(user_id)))
            self.table_users.setItem(idx, 1, QTableWidgetItem(uname))
            self.table_users.setItem(idx, 2, QTableWidgetItem(role.capitalize()))
            self.table_users.setItem(idx, 3, QTableWidgetItem("Active" if active else "Disabled"))

    def save_user(self):
        username = self.txt_username.text().strip()
        password = self.txt_password.text().strip()
        role = self.cmb_role.currentText().lower()
        
        if not username or not password:
            QMessageBox.warning(self, "Validation Error", "Username and Password are required!")
            return
            
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO users (username, password_hash, role, is_active) VALUES (?, ?, ?, 1)", 
                       (username, password, role))
        self.conn.commit()
        
        QMessageBox.information(self, "Success", f"User '{username}' saved successfully!")
        self.load_users()
        self.txt_username.clear()
        self.txt_password.clear()