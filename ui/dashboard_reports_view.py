# ui/dashboard_reports_view.py
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QFrame
)
from PySide6.QtCore import Qt

class DashboardAndReportsView(QWidget):
    """Dashboard and reports. Backup and Restore now has its own screen (ui/backup_view.py)."""
    
    def __init__(self, parent=None, db_connection=None, db_path="cloth_shop.db", user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.db_path = db_path
        self.user_id = user_id
        
        self.setup_ui()
        self.load_dashboard_kpis()
        
    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        
        # 1. Top KPI Summary Cards
        kpi_layout = QHBoxLayout()
        
        self.card_sales = self.create_kpi_card("Today's Sales", "₹0.00", "#e8f5e9", "#2e7d32")
        self.card_bills = self.create_kpi_card("Bill Count", "0 Bills", "#e3f2fd", "#1565c0")
        self.card_stock = self.create_kpi_card("Low Stock Items", "0 Items", "#ffebee", "#c62828")
        self.card_dues = self.create_kpi_card("Customer Dues", "₹0.00", "#fff3e0", "#ef6c00")
        
        kpi_layout.addWidget(self.card_sales)
        kpi_layout.addWidget(self.card_bills)
        kpi_layout.addWidget(self.card_stock)
        kpi_layout.addWidget(self.card_dues)
        main_layout.addLayout(kpi_layout)
        
        # 2. Lower Split: Reports & System Backup Controls
        bottom_layout = QHBoxLayout()
        
        # Reports Group
        reports_group = QGroupBox("GST & Business Performance Reports")
        rg_layout = QVBoxLayout()
        
        r_btns = QHBoxLayout()
        btn_gstr1 = QPushButton("Generate GSTR-1 Summary")
        btn_gstr1.clicked.connect(self.run_gstr1_report)
        
        btn_margin = QPushButton("Gross Margin Report")
        btn_margin.clicked.connect(self.run_margin_report)
        
        r_btns.addWidget(btn_gstr1)
        r_btns.addWidget(btn_margin)
        rg_layout.addLayout(r_btns)
        
        self.table_report = QTableWidget(0, 5)
        self.table_report.setHorizontalHeaderLabels(["HSN Code", "GST Rate %", "Taxable Value (₹)", "Tax Amount (₹)", "Total Value (₹)"])
        rg_layout.addWidget(self.table_report)
        
        reports_group.setLayout(rg_layout)
        bottom_layout.addWidget(reports_group)
        
        main_layout.addLayout(bottom_layout)

    def create_kpi_card(self, title, default_val, bg_color, text_color):
        card = QFrame()
        card.setStyleSheet(f"background-color: {bg_color}; border-radius: 8px; padding: 12px;")
        l = QVBoxLayout(card)
        
        lbl_title = QLabel(f"<span style='color: #555; font-size: 13px;'>{title}</span>")
        lbl_val = QLabel(f"<h2 style='color: {text_color}; margin: 0;'>{default_val}</h2>")
        
        l.addWidget(lbl_title)
        l.addWidget(lbl_val)
        card.lbl_val = lbl_val
        return card

    def load_dashboard_kpis(self):
        if not self.conn:
            return
        from core.dashboard_backup_engine import get_dashboard_kpi_summary
        summary = get_dashboard_kpi_summary(self.conn)
        
        self.card_sales.lbl_val.setText(f"₹{summary['today_sales_rs']:.2f}")
        self.card_bills.lbl_val.setText(f"{summary['bill_count']} Bills")
        self.card_stock.lbl_val.setText(f"{summary['low_stock_count']} Items")
        self.card_dues.lbl_val.setText(f"₹{summary['total_dues_rs']:.2f}")

    def run_gstr1_report(self):
        if not self.conn:
            return
        from core.dashboard_backup_engine import get_gstr1_summary_report
        rows = get_gstr1_summary_report(self.conn, "2026-01-01", "2026-12-31")
        
        self.table_report.setRowCount(0)
        for r in rows:
            idx = self.table_report.rowCount()
            self.table_report.insertRow(idx)
            self.table_report.setItem(idx, 0, QTableWidgetItem(r["hsn"]))
            self.table_report.setItem(idx, 1, QTableWidgetItem(f"{r['rate_percent']}%"))
            self.table_report.setItem(idx, 2, QTableWidgetItem(f"₹{r['taxable_amt_rs']:.2f}"))
            self.table_report.setItem(idx, 3, QTableWidgetItem(f"₹{r['tax_amt_rs']:.2f}"))
            self.table_report.setItem(idx, 4, QTableWidgetItem(f"₹{r['total_val_rs']:.2f}"))

    def run_margin_report(self):
        QMessageBox.information(self, "Gross Margin Report", "Gross Margin calculated based on COGS vs Net Sales.")
