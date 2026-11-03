# ui/customers_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt

class CustomersView(QWidget):
    """Dedicated Customer Directory & Ledger Dues View."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        self.setup_ui()
        self.load_customers()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        
        # Left Panel: Add New Customer
        left_group = QGroupBox("Add / Edit Customer")
        lg_layout = QVBoxLayout()
        
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("Customer Name")
        
        self.txt_phone = QLineEdit()
        self.txt_phone.setPlaceholderText("Phone / Mobile Number")
        
        self.txt_address = QLineEdit()
        self.txt_address.setPlaceholderText("Address / City")
        
        btn_save = QPushButton("+ Save Customer")
        btn_save.setStyleSheet("background-color: #1565c0; color: white; font-weight: bold; padding: 10px;")
        btn_save.clicked.connect(self.save_customer)
        
        lg_layout.addWidget(QLabel("Customer Name:"))
        lg_layout.addWidget(self.txt_name)
        lg_layout.addWidget(QLabel("Phone Number:"))
        lg_layout.addWidget(self.txt_phone)
        lg_layout.addWidget(QLabel("Address:"))
        lg_layout.addWidget(self.txt_address)
        lg_layout.addWidget(btn_save)
        lg_layout.addStretch()
        left_group.setLayout(lg_layout)
        
        # Right Panel: Customer Directory Table
        right_group = QGroupBox("Customer Directory & Due Balance")
        rg_layout = QVBoxLayout()
        
        self.table_customers = QTableWidget(0, 4)
        self.table_customers.setHorizontalHeaderLabels(["ID", "Customer Name", "Phone", "Address"])
        rg_layout.addWidget(self.table_customers)
        right_group.setLayout(rg_layout)
        
        layout.addWidget(left_group, stretch=4)
        layout.addWidget(right_group, stretch=6)

    def load_customers(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT id, name, phone, address FROM customers")
            rows = cursor.fetchall()
            self.table_customers.setRowCount(0)
            for c_id, name, phone, addr in rows:
                idx = self.table_customers.rowCount()
                self.table_customers.insertRow(idx)
                self.table_customers.setItem(idx, 0, QTableWidgetItem(str(c_id)))
                self.table_customers.setItem(idx, 1, QTableWidgetItem(str(name or "")))
                self.table_customers.setItem(idx, 2, QTableWidgetItem(str(phone or "")))
                self.table_customers.setItem(idx, 3, QTableWidgetItem(str(addr or "")))
        except Exception as e:
            print("Error loading customers:", e)

    def save_customer(self):
        name = self.txt_name.text().strip()
        phone = self.txt_phone.text().strip()
        address = self.txt_address.text().strip()
        
        if not name:
            QMessageBox.warning(self, "Error", "Customer Name is required!")
            return
            
        cursor = self.conn.cursor()
        try:
            cursor.execute("INSERT INTO customers (name, phone, address) VALUES (?, ?, ?)",
                           (name, phone, address))
            self.conn.commit()
            QMessageBox.information(self, "Success", f"Customer '{name}' added successfully!")
            self.txt_name.clear()
            self.txt_phone.clear()
            self.txt_address.clear()
            self.load_customers()
        except Exception as e:
            QMessageBox.warning(self, "Database Error", str(e))