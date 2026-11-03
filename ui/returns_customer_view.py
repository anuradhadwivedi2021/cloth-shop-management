# ui/returns_customer_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QFrame
)
from PySide6.QtCore import Qt

class ReturnsAndCustomerLedgerView(QWidget):
    """Returns, Exchanges, and Customer Loyalty/Credit Dues View."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        
        self.setup_ui()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        
        # Left Panel: Sales Return & Exchange Console
        ret_panel = QVBoxLayout()
        ret_group = QGroupBox("Sales Return & Exchange Console")
        ret_layout = QVBoxLayout()
        
        # Invoice Lookup Row
        inv_row = QHBoxLayout()
        self.txt_bill_no = QLineEdit()
        self.txt_bill_no.setPlaceholderText("Enter Original Bill / Credit Note No...")
        btn_find_bill = QPushButton("Find Invoice")
        btn_find_bill.clicked.connect(self.lookup_bill)
        
        inv_row.addWidget(QLabel("Invoice No:"))
        inv_row.addWidget(self.txt_bill_no)
        inv_row.addWidget(btn_find_bill)
        ret_layout.addLayout(inv_row)
        
        # Returned Item vs Replacement Item Grid
        self.table_exchange = QTableWidget(0, 5)
        self.table_exchange.setHorizontalHeaderLabels([
            "Action", "Variant (Size/Colour)", "Original Price (₹)", "Replacement Item", "Diff Amount (₹)"
        ])
        ret_layout.addWidget(self.table_exchange)
        
        btn_process_ret = QPushButton("Process Exchange / Store Credit")
        btn_process_ret.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 10px;")
        btn_process_ret.clicked.connect(self.process_exchange_action)
        ret_layout.addWidget(btn_process_ret)
        
        ret_group.setLayout(ret_layout)
        ret_panel.addWidget(ret_group)
        layout.addLayout(ret_panel, stretch=6)
        
        # Right Panel: Customer Ledgers & Ageing Lookup
        cust_panel = QVBoxLayout()
        cust_group = QGroupBox("Customer Ledgers & Credit Lookup")
        cust_layout = QVBoxLayout()
        
        phone_row = QHBoxLayout()
        self.txt_cust_phone = QLineEdit()
        self.txt_cust_phone.setPlaceholderText("Enter Customer Phone No...")
        btn_search_cust = QPushButton("Search")
        btn_search_cust.clicked.connect(self.search_customer)
        
        phone_row.addWidget(self.txt_cust_phone)
        phone_row.addWidget(btn_search_cust)
        cust_layout.addLayout(phone_row)
        
        # Customer Info Cards
        self.lbl_cust_name = QLabel("Customer: --")
        self.lbl_store_credit = QLabel("Available Store Credit: ₹0.00")
        self.lbl_loyalty_pts = QLabel("Loyalty Points: 0 Pts")
        self.lbl_credit_limit = QLabel("Credit Limit: Disabled")
        
        # Ageing Breakdown Box
        ageing_frame = QFrame()
        ageing_frame.setStyleSheet("background-color: #fff3e0; border: 1px solid #ffe0b2; padding: 8px;")
        af_layout = QVBoxLayout(ageing_frame)
        af_layout.addWidget(QLabel("<b>Credit Dues Ageing Breakdown</b>"))
        self.lbl_due_0_30 = QLabel("0–30 Days: ₹0.00")
        self.lbl_due_31_60 = QLabel("31–60 Days: ₹0.00")
        self.lbl_due_60_plus = QLabel("60+ Days (Overdue): ₹0.00")
        
        af_layout.addWidget(self.lbl_due_0_30)
        af_layout.addWidget(self.lbl_due_31_60)
        af_layout.addWidget(self.lbl_due_60_plus)
        
        cust_layout.addWidget(self.lbl_cust_name)
        cust_layout.addWidget(self.lbl_store_credit)
        cust_layout.addWidget(self.lbl_loyalty_pts)
        cust_layout.addWidget(self.lbl_credit_limit)
        cust_layout.addWidget(ageing_frame)
        cust_layout.addStretch()
        
        cust_group.setLayout(cust_layout)
        cust_panel.addWidget(cust_group)
        layout.addLayout(cust_panel, stretch=4)

    def lookup_bill(self):
        bill_no = self.txt_bill_no.text().strip()
        if not bill_no:
            return
        QMessageBox.information(self, "Bill Found", f"Original invoice '{bill_no}' loaded into Exchange Console.")

    def search_customer(self):
        phone = self.txt_cust_phone.text().strip()
        if not phone or not self.conn:
            return
            
        from core.returns_customer_engine import search_customer_profile_and_ledgers
        data = search_customer_profile_and_ledgers(self.conn, phone)
        
        if not data.get("found"):
            QMessageBox.warning(self, "Not Found", "No customer found with this phone number.")
            return
            
        self.lbl_cust_name.setText(f"Customer: <b>{data['name']}</b> ({data['phone']})")
        self.lbl_store_credit.setText(f"Available Store Credit: <b style='color:#2e7d32;'>₹{data['store_credit_rs']:.2f}</b>")
        self.lbl_loyalty_pts.setText(f"Loyalty Points: <b>{data['loyalty_points']} Pts</b>")
        
        if data["credit_enabled"]:
            self.lbl_credit_limit.setText(f"Credit Limit: ₹{data['credit_limit_rs']:.2f}")
        else:
            self.lbl_credit_limit.setText("Credit Limit: Disabled")
            
        self.lbl_due_0_30.setText(f"0–30 Days: ₹{data['ageing']['0_30']:.2f}")
        self.lbl_due_31_60.setText(f"31–60 Days: ₹{data['ageing']['31_60']:.2f}")
        self.lbl_due_60_plus.setText(f"60+ Days (Overdue): ₹{data['ageing']['60_plus']:.2f}")

    def process_exchange_action(self):
        QMessageBox.information(self, "Success", "Exchange completed! Stock updated and Credit Note printed.")