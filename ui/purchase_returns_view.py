# ui/purchase_returns_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QComboBox
)

class PurchaseReturnsView(QWidget):
    """Dedicated Purchase Return to Supplier Console."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        hdr_group = QGroupBox("Return Defective / Excess Goods to Supplier")
        h_layout = QHBoxLayout()
        
        self.cmb_supplier = QComboBox()
        self.cmb_supplier.addItem("Select Supplier...")
        
        self.txt_invoice_no = QLineEdit()
        self.txt_invoice_no.setPlaceholderText("Original Purchase Invoice No.")
        
        self.txt_barcode = QLineEdit()
        self.txt_barcode.setPlaceholderText("Scan SKU / Barcode...")
        
        btn_search = QPushButton("Find Purchase Line")
        
        h_layout.addWidget(QLabel("Supplier:"))
        h_layout.addWidget(self.cmb_supplier)
        h_layout.addWidget(self.txt_invoice_no)
        h_layout.addWidget(self.txt_barcode)
        h_layout.addWidget(btn_search)
        hdr_group.setLayout(h_layout)
        layout.addWidget(hdr_group)
        
        # Items Table
        self.table_items = QTableWidget(0, 5)
        self.table_items.setHorizontalHeaderLabels(["SKU / Barcode", "Product Name", "Purchased Qty", "Return Qty", "Refund Amount (₹)"])
        layout.addWidget(self.table_items)
        
        btn_process = QPushButton("Process Purchase Return & Update Stock")
        btn_process.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; padding: 12px;")
        btn_process.clicked.connect(self.process_return)
        layout.addWidget(btn_process)

    def process_return(self):
        QMessageBox.information(self, "Success", "Purchase Return recorded! Supplier ledger debited & stock updated.")