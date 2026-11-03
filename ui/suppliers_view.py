# ui/suppliers_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt

class SuppliersManagementView(QWidget):
    """Dedicated Supplier Directory & Dues View."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        self.setup_ui()
        self.load_suppliers()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        
        # Left Panel: Add New Supplier
        left_group = QGroupBox("Add / Edit Supplier")
        lg_layout = QVBoxLayout()
        
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("Supplier / Vendor Name")
        
        self.txt_phone = QLineEdit()
        self.txt_phone.setPlaceholderText("Phone / Contact Number")
        
        self.txt_gstin = QLineEdit()
        self.txt_gstin.setPlaceholderText("Supplier GSTIN")
        
        btn_save = QPushButton("+ Add Supplier")
        btn_save.setStyleSheet("background-color: #1565c0; color: white; font-weight: bold; padding: 10px;")
        btn_save.clicked.connect(self.save_supplier)
        
        lg_layout.addWidget(QLabel("Supplier Name:"))
        lg_layout.addWidget(self.txt_name)
        lg_layout.addWidget(QLabel("Phone Number:"))
        lg_layout.addWidget(self.txt_phone)
        lg_layout.addWidget(QLabel("GSTIN:"))
        lg_layout.addWidget(self.txt_gstin)
        lg_layout.addWidget(btn_save)
        lg_layout.addStretch()
        left_group.setLayout(lg_layout)
        
        # Right Panel: Supplier List Table
        right_group = QGroupBox("Supplier Directory & Dues")
        rg_layout = QVBoxLayout()
        
        self.table_suppliers = QTableWidget(0, 4)
        self.table_suppliers.setHorizontalHeaderLabels(["ID", "Supplier Name", "Phone", "GSTIN"])
        rg_layout.addWidget(self.table_suppliers)
        right_group.setLayout(rg_layout)
        
        layout.addWidget(left_group, stretch=4)
        layout.addWidget(right_group, stretch=6)

    def load_suppliers(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT id, name, phone, gstin FROM suppliers")
            rows = cursor.fetchall()
            self.table_suppliers.setRowCount(0)
            for s_id, name, phone, gstin in rows:
                idx = self.table_suppliers.rowCount()
                self.table_suppliers.insertRow(idx)
                self.table_suppliers.setItem(idx, 0, QTableWidgetItem(str(s_id)))
                self.table_suppliers.setItem(idx, 1, QTableWidgetItem(str(name or "")))
                self.table_suppliers.setItem(idx, 2, QTableWidgetItem(str(phone or "")))
                self.table_suppliers.setItem(idx, 3, QTableWidgetItem(str(gstin or "")))
        except Exception as e:
            print("Error loading suppliers:", e)

    def save_supplier(self):
        name = self.txt_name.text().strip()
        phone = self.txt_phone.text().strip()
        gstin = self.txt_gstin.text().strip()
        
        if not name:
            QMessageBox.warning(self, "Error", "Supplier Name is required!")
            return
            
        cursor = self.conn.cursor()
        try:
            cursor.execute("INSERT INTO suppliers (name, phone, gstin) VALUES (?, ?, ?)",
                           (name, phone, gstin))
            self.conn.commit()
            QMessageBox.information(self, "Success", f"Supplier '{name}' added successfully!")
            self.txt_name.clear()
            self.txt_phone.clear()
            self.txt_gstin.clear()
            self.load_suppliers()
        except Exception as e:
            QMessageBox.warning(self, "Database Error", str(e))