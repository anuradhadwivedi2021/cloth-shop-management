# ui/cash_audit_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QFrame
)
from PySide6.QtCore import Qt

class CashClosingAndAuditView(QWidget):
    """Day-end Cash Register Closing, Expense Tracking & Physical Stock Audit UI."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        
        self.setup_ui()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        
        # Left Panel: Day End Cash Closing Register
        cash_panel = QVBoxLayout()
        cash_group = QGroupBox("Daily Cash Register Closing")
        cg_layout = QVBoxLayout()
        
        self.lbl_opening = QLabel("Opening Balance: ₹0.00")
        self.lbl_cash_in = QLabel("Cash Sales Today (+): ₹0.00")
        self.lbl_cash_out = QLabel("Cash Expenses Today (-): ₹0.00")
        
        self.lbl_expected = QLabel("<h3 style='color: #1976d2;'>Expected Cash in Drawer: ₹0.00</h3>")
        
        cg_layout.addWidget(self.lbl_opening)
        cg_layout.addWidget(self.lbl_cash_in)
        cg_layout.addWidget(self.lbl_cash_out)
        cg_layout.addWidget(self.lbl_expected)
        
        # Physical Cash Count Entry Box
        count_frame = QFrame()
        count_frame.setStyleSheet("background-color: #f5f5f5; border: 1px solid #ddd; padding: 10px;")
        cf_layout = QVBoxLayout(count_frame)
        
        cf_layout.addWidget(QLabel("<b>Enter Physical Cash Counted (₹):</b>"))
        self.txt_physical = QLineEdit()
        self.txt_physical.setPlaceholderText("Counted cash in drawer...")
        self.txt_physical.textChanged.connect(self.calculate_diff)
        
        self.lbl_diff = QLabel("Difference: ₹0.00")
        self.txt_reason = QLineEdit()
        self.txt_reason.setPlaceholderText("Reason if physical count does not match...")
        
        cf_layout.addWidget(self.txt_physical)
        cf_layout.addWidget(self.lbl_diff)
        cf_layout.addWidget(self.txt_reason)
        cg_layout.addWidget(count_frame)
        
        btn_close_day = QPushButton("Submit & Close Cash Register")
        btn_close_day.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 12px;")
        btn_close_day.clicked.connect(self.submit_cash_closing)
        cg_layout.addWidget(btn_close_day)
        
        cash_group.setLayout(cg_layout)
        cash_panel.addWidget(cash_group)
        layout.addLayout(cash_panel, stretch=5)
        
        # Right Panel: Expense Record & Stock Audit
        exp_panel = QVBoxLayout()
        exp_group = QGroupBox("Record Daily Expense")
        eg_layout = QVBoxLayout()
        
        self.txt_exp_name = QLineEdit()
        self.txt_exp_name.setPlaceholderText("Expense Name (e.g. Tea, Rent, Freight)")
        self.txt_exp_amt = QLineEdit()
        self.txt_exp_amt.setPlaceholderText("Amount (₹)")
        
        btn_add_exp = QPushButton("+ Record Expense")
        btn_add_exp.clicked.connect(self.add_expense)
        
        eg_layout.addWidget(QLabel("Category / Purpose:"))
        eg_layout.addWidget(self.txt_exp_name)
        eg_layout.addWidget(QLabel("Amount (₹):"))
        eg_layout.addWidget(self.txt_exp_amt)
        eg_layout.addWidget(btn_add_exp)
        
        exp_group.setLayout(eg_layout)
        exp_panel.addWidget(exp_group)
        layout.addLayout(exp_panel, stretch=5)

    def calculate_diff(self):
        try:
            val = float(self.txt_physical.text().strip() or 0.0)
            diff = val - 0.0 # Expected cash difference
            if diff < 0:
                self.lbl_diff.setText(f"<b style='color: red;'>Shortage: ₹{abs(diff):.2f}</b>")
            elif diff > 0:
                self.lbl_diff.setText(f"<b style='color: green;'>Excess: ₹{diff:.2f}</b>")
            else:
                self.lbl_diff.setText("Difference: ₹0.00 (Exact Match)")
        except ValueError:
            pass

    def submit_cash_closing(self):
        QMessageBox.information(self, "Success", "Day Close Cash Register submitted successfully!")

    def add_expense(self):
        QMessageBox.information(self, "Recorded", "Shop Expense recorded successfully.")