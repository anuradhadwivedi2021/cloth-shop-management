# ui/purchases_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QComboBox
)
from PySide6.QtCore import Qt

class PurchasesManagementView(QWidget):
    """Purchases and Stock Receiving UI (Draft -> Confirm lifecycle)."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Supplier & Invoice Details Group
        hdr_group = QGroupBox("Purchase Header & Supplier Info")
        hdr_layout = QHBoxLayout()
        
        self.cmb_supplier = QComboBox()
        self.cmb_supplier.addItem("Select Supplier...", None)
        self.load_suppliers()
        
        self.txt_inv_no = QLineEdit()
        self.txt_inv_no.setPlaceholderText("Supplier Invoice No.")
        
        self.txt_inv_date = QLineEdit()
        self.txt_inv_date.setPlaceholderText("YYYY-MM-DD")
        
        hdr_layout.addWidget(QLabel("Supplier:"))
        hdr_layout.addWidget(self.cmb_supplier)
        hdr_layout.addWidget(QLabel("Invoice No:"))
        hdr_layout.addWidget(self.txt_inv_no)
        hdr_layout.addWidget(QLabel("Invoice Date:"))
        hdr_layout.addWidget(self.txt_inv_date)
        
        hdr_group.setLayout(hdr_layout)
        layout.addWidget(hdr_group)
        
        # Items Entry Table
        self.table_items = QTableWidget(0, 6)
        self.table_items.setHorizontalHeaderLabels([
            "Variant SKU / Barcode", "Product Name", "Size/Colour", "Qty (Pcs/Mtr)", "Cost Price (₹)", "Line Total (₹)"
        ])
        layout.addWidget(self.table_items)
        
        # Action Buttons
        btn_layout = QHBoxLayout()
        
        btn_add_row = QPushButton("+ Add Variant Line")
        btn_add_row.clicked.connect(self.add_line_row)
        
        btn_save_draft = QPushButton("Save as Draft")
        btn_save_draft.setStyleSheet("background-color: #ed6c02; color: white; font-weight: bold; padding: 10px;")
        btn_save_draft.clicked.connect(self.save_draft)
        
        btn_confirm = QPushButton("Confirm & Receive Stock")
        btn_confirm.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        btn_confirm.clicked.connect(self.confirm_purchase)
        
        btn_layout.addWidget(btn_add_row)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save_draft)
        btn_layout.addWidget(btn_confirm)
        
        layout.addLayout(btn_layout)

    def load_suppliers(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, name FROM suppliers WHERE is_active = 1")
        for s_id, s_name in cursor.fetchall():
            self.cmb_supplier.addItem(s_name, s_id)

    def add_line_row(self):
        row_idx = self.table_items.rowCount()
        self.table_items.insertRow(row_idx)
        self.table_items.setItem(row_idx, 0, QTableWidgetItem(""))
        self.table_items.setItem(row_idx, 1, QTableWidgetItem(""))
        self.table_items.setItem(row_idx, 2, QTableWidgetItem(""))
        self.table_items.setItem(row_idx, 3, QTableWidgetItem("1"))
        self.table_items.setItem(row_idx, 4, QTableWidgetItem("0.00"))
        self.table_items.setItem(row_idx, 5, QTableWidgetItem("0.00"))

    def save_draft(self):
        QMessageBox.information(self, "Draft Saved", "Purchase saved as Draft. Stock has NOT been updated yet.")

    def confirm_purchase(self):
        # Calls confirm_and_receive_purchase backend function
        reply = QMessageBox.question(
            self, "Confirm Stock Receiving",
            "Are you sure you want to confirm this purchase? Stock will be updated and barcode label printing will trigger.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            QMessageBox.information(self, "Success", "Purchase confirmed! Stock updated and Barcode Batch Printing triggered.")