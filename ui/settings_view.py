# ui/settings_view.py
import sqlite3
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QMessageBox, QGroupBox, QComboBox, QTextEdit, QScrollArea
)
from PySide6.QtCore import Qt

class SettingsView(QWidget):
    """Shop Settings, Invoice Customization, Thermal Printer & System Configurations View."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        
        self.setup_ui()
        self.load_settings()
        
    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        
        # Scroll Area for clean layout
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # 1. Shop Profile & GST Details
        shop_group = QGroupBox("Shop Profile & Invoice Header Details")
        sg_layout = QVBoxLayout()
        
        self.txt_shop_name = QLineEdit()
        self.txt_shop_name.setPlaceholderText("Shop Name (e.g., Anu Fashion)")
        
        self.txt_gstin = QLineEdit()
        self.txt_gstin.setPlaceholderText("GSTIN Number (15 Digits)")
        
        self.txt_phone = QLineEdit()
        self.txt_phone.setPlaceholderText("Contact Phone / Mobile")
        
        self.txt_address = QTextEdit()
        self.txt_address.setPlaceholderText("Full Address to print on Thermal Receipts & Tax Invoices...")
        self.txt_address.setMaximumHeight(70)
        
        sg_layout.addWidget(QLabel("Shop Name:"))
        sg_layout.addWidget(self.txt_shop_name)
        sg_layout.addWidget(QLabel("GSTIN Number:"))
        sg_layout.addWidget(self.txt_gstin)
        sg_layout.addWidget(QLabel("Contact Phone:"))
        sg_layout.addWidget(self.txt_phone)
        sg_layout.addWidget(QLabel("Shop Address:"))
        sg_layout.addWidget(self.txt_address)
        
        shop_group.setLayout(sg_layout)
        scroll_layout.addWidget(shop_group)
        
        # 2. Billing & Thermal Printer Preferences
        pos_group = QGroupBox("POS Billing & Printer Preferences")
        pg_layout = QVBoxLayout()
        
        self.cmb_printer_width = QComboBox()
        self.cmb_printer_width.addItems(["80mm (Standard Thermal)", "58mm (Small Thermal)", "A4/A5 PDF Invoice"])
        
        self.txt_terms = QTextEdit()
        self.txt_terms.setPlaceholderText("Invoice Footer Terms (e.g., Goods once sold will not be taken back without bill...)")
        self.txt_terms.setMaximumHeight(60)
        
        pg_layout.addWidget(QLabel("Receipt / Printer Paper Size:"))
        pg_layout.addWidget(self.cmb_printer_width)
        pg_layout.addWidget(QLabel("Invoice Terms & Conditions:"))
        pg_layout.addWidget(self.txt_terms)
        
        pos_group.setLayout(pg_layout)
        scroll_layout.addWidget(pos_group)
        
        # 3. Credit & Security Settings
        sys_group = QGroupBox("Credit Sales & Owner Overrides")
        sys_layout = QVBoxLayout()
        
        self.txt_default_credit_days = QLineEdit()
        self.txt_default_credit_days.setPlaceholderText("Default Credit Payment Days (e.g. 30)")
        
        self.txt_backup_path = QLineEdit()
        self.txt_backup_path.setPlaceholderText("Default Backup Folder Path...")
        self.txt_backup_path.setText("backups/")
        
        sys_layout.addWidget(QLabel("Default Customer Credit Period (Days):"))
        sys_layout.addWidget(self.txt_default_credit_days)
        sys_layout.addWidget(QLabel("Encrypted Backup Storage Path:"))
        sys_layout.addWidget(self.txt_backup_path)
        
        sys_group.setLayout(sys_layout)
        scroll_layout.addWidget(sys_group)
        
        # Save Button
        btn_save = QPushButton("Save Settings & Apply Changes")
        btn_save.setStyleSheet("background-color: #1565c0; color: white; font-weight: bold; padding: 12px; font-size: 14px;")
        btn_save.clicked.connect(self.save_settings)
        scroll_layout.addWidget(btn_save)
        
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll)

    def load_settings(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        settings_dict = dict(cursor.fetchall())
        
        self.txt_shop_name.setText(settings_dict.get("shop_name", "anu fashion"))
        self.txt_gstin.setText(settings_dict.get("gstin", ""))
        self.txt_phone.setText(settings_dict.get("phone", ""))
        self.txt_address.setText(settings_dict.get("address", ""))
        self.txt_terms.setText(settings_dict.get("invoice_terms", "Thank you for shopping with us!"))
        self.txt_default_credit_days.setText(settings_dict.get("default_credit_days", "30"))

    def save_settings(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        settings_to_save = {
            "shop_name": self.txt_shop_name.text().strip(),
            "gstin": self.txt_gstin.text().strip(),
            "phone": self.txt_phone.text().strip(),
            "address": self.txt_address.toPlainText().strip(),
            "invoice_terms": self.txt_terms.toPlainText().strip(),
            "default_credit_days": self.txt_default_credit_days.text().strip(),
            "printer_type": self.cmb_printer_width.currentText()
        }
        
        for k, v in settings_to_save.items():
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
            
        self.conn.commit()
        QMessageBox.information(self, "Success", "Shop Settings updated successfully!")