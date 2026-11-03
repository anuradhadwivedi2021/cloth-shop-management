# ui/expenses_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QComboBox
)
from PySide6.QtCore import Qt

from core.money import parse_rupees

class ExpensesView(QWidget):
    """Dedicated Daily Shop Expenses Management View."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        self.setup_ui()
        self.load_expenses()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        
        # Left Panel: Add Expense Form
        form_group = QGroupBox("Record New Expense")
        fg_layout = QVBoxLayout()
        
        self.cmb_category = QComboBox()
        self.cmb_category.addItems(["Tea & Snacks", "Shop Rent", "Electricity", "Packaging & Bags", "Staff Salary/Advance", "Miscellaneous"])
        
        self.txt_amount = QLineEdit()
        self.txt_amount.setPlaceholderText("Amount (₹)")
        
        self.txt_notes = QLineEdit()
        self.txt_notes.setPlaceholderText("Details / Remarks...")
        
        btn_save = QPushButton("+ Add Expense Entry")
        btn_save.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 10px;")
        btn_save.clicked.connect(self.save_expense)
        
        fg_layout.addWidget(QLabel("Category:"))
        fg_layout.addWidget(self.cmb_category)
        fg_layout.addWidget(QLabel("Amount (₹):"))
        fg_layout.addWidget(self.txt_amount)
        fg_layout.addWidget(QLabel("Notes:"))
        fg_layout.addWidget(self.txt_notes)
        fg_layout.addWidget(btn_save)
        fg_layout.addStretch()
        form_group.setLayout(fg_layout)
        
        # Right Panel: Expenses Table
        list_group = QGroupBox("Today's Recorded Expenses")
        lg_layout = QVBoxLayout()
        
        self.table_expenses = QTableWidget(0, 4)
        self.table_expenses.setHorizontalHeaderLabels(["ID", "Category", "Amount (₹)", "Notes"])
        lg_layout.addWidget(self.table_expenses)
        list_group.setLayout(lg_layout)
        
        layout.addWidget(form_group, stretch=4)
        layout.addWidget(list_group, stretch=6)

    def load_expenses(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, category, amount_paise / 100.0, notes FROM expenses WHERE date(created_at) = date('now')")
        rows = cursor.fetchall()
        self.table_expenses.setRowCount(0)
        for r_id, cat, amt, notes in rows:
            idx = self.table_expenses.rowCount()
            self.table_expenses.insertRow(idx)
            self.table_expenses.setItem(idx, 0, QTableWidgetItem(str(r_id)))
            self.table_expenses.setItem(idx, 1, QTableWidgetItem(cat))
            self.table_expenses.setItem(idx, 2, QTableWidgetItem(f"₹{amt:.2f}"))
            self.table_expenses.setItem(idx, 3, QTableWidgetItem(notes or ""))

    def save_expense(self):
        try:
            amt_paise = parse_rupees(self.txt_amount.text(), allow_zero=False)
        except ValueError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO expenses (category, amount_paise, notes, user_id) VALUES (?, ?, ?, ?)",
                           (self.cmb_category.currentText(), amt_paise, self.txt_notes.text().strip(), self.user_id))
            self.conn.commit()
            QMessageBox.information(self, "Success", "Expense recorded!")
            self.txt_amount.clear()
            self.txt_notes.clear()
            self.load_expenses()
        except ValueError as exc:
            QMessageBox.warning(self, "Error", str(exc))