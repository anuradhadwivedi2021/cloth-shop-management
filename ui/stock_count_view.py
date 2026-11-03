# ui/stock_count_view.py
import csv
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QFileDialog, QComboBox
)
from PySide6.QtCore import Qt

class StockCountView(QWidget):
    """Excel Import/Export, Variant Pricing (Size/Color/Brand/Category) & Stock In/Out Management."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        self.setup_ui()
        self.load_stock_data()
        
    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        
        # 1. Top Action Bar: Excel Import / Export & Filters
        action_group = QGroupBox("Excel Operations & Stock Management")
        a_layout = QHBoxLayout()
        
        btn_export = QPushButton("📥 Export Stock to CSV/Excel")
        btn_export.setStyleSheet("background-color: #1976d2; color: white; font-weight: bold; padding: 8px;")
        btn_export.clicked.connect(self.export_to_excel)
        
        btn_import = QPushButton("📤 Import Stock from CSV/Excel")
        btn_import.setStyleSheet("background-color: #388e3c; color: white; font-weight: bold; padding: 8px;")
        btn_import.clicked.connect(self.import_from_excel)
        
        a_layout.addWidget(btn_export)
        a_layout.addWidget(btn_import)
        a_layout.addStretch()
        action_group.setLayout(a_layout)
        main_layout.addWidget(action_group)

        # 2. Middle Panel: Stock In / Stock Out & Variant Price Update Form
        form_group = QGroupBox("Variant Pricing & Stock In / Out Movement")
        f_layout = QHBoxLayout()
        
        self.txt_barcode = QLineEdit()
        self.txt_barcode.setPlaceholderText("Scan/Enter Barcode or SKU...")
        
        self.cmb_action = QComboBox()
        self.cmb_action.addItems(["Stock In (+)", "Stock Out (-)"])
        
        self.txt_qty = QLineEdit()
        self.txt_qty.setPlaceholderText("Quantity (Pcs)")
        
        self.txt_new_price = QLineEdit()
        self.txt_new_price.setPlaceholderText("Set Variant Price (₹)")
        
        btn_apply = QPushButton("Update Stock & Price")
        btn_apply.setStyleSheet("background-color: #ed6c02; color: white; font-weight: bold; padding: 8px;")
        btn_apply.clicked.connect(self.update_stock_and_price)
        
        f_layout.addWidget(QLabel("Barcode/SKU:"))
        f_layout.addWidget(self.txt_barcode)
        f_layout.addWidget(QLabel("Movement:"))
        f_layout.addWidget(self.cmb_action)
        f_layout.addWidget(QLabel("Qty:"))
        f_layout.addWidget(self.txt_qty)
        f_layout.addWidget(QLabel("Variant Price (₹):"))
        f_layout.addWidget(self.txt_new_price)
        f_layout.addWidget(btn_apply)
        
        form_group.setLayout(f_layout)
        main_layout.addWidget(form_group)

        # 3. Stock Table (Category, Brand, Variant Size/Color, Price, Stock In/Out Qty)
        self.table_stock = QTableWidget(0, 8)
        self.table_stock.setHorizontalHeaderLabels([
            "Barcode / SKU", "Product Name", "Category", "Brand", "Size", "Color", "Selling Price (₹)", "Current Stock"
        ])
        main_layout.addWidget(self.table_stock)

    def load_stock_data(self):
        if not self.conn:
            return
        cursor = self.conn.cursor()
        try:
            # Query joining Product and Product Variants with Categories & Brands
            query = """
                SELECT v.barcode, p.name, COALESCE(c.name, 'General'), COALESCE(b.name, 'Generic'), 
                       v.size, v.color, (v.price_paise / 100.0), v.stock_qty
                FROM product_variants v
                JOIN products p ON v.product_id = p.id
                LEFT JOIN categories c ON p.category_id = c.id
                LEFT JOIN brands b ON p.brand_id = b.id
            """
            cursor.execute(query)
            rows = cursor.fetchall()
            
            self.table_stock.setRowCount(0)
            for barcode, name, cat, brand, size, color, price, qty in rows:
                idx = self.table_stock.rowCount()
                self.table_stock.insertRow(idx)
                self.table_stock.setItem(idx, 0, QTableWidgetItem(str(barcode or "")))
                self.table_stock.setItem(idx, 1, QTableWidgetItem(str(name or "")))
                self.table_stock.setItem(idx, 2, QTableWidgetItem(str(cat or "")))
                self.table_stock.setItem(idx, 3, QTableWidgetItem(str(brand or "")))
                self.table_stock.setItem(idx, 4, QTableWidgetItem(str(size or "")))
                self.table_stock.setItem(idx, 5, QTableWidgetItem(str(color or "")))
                self.table_stock.setItem(idx, 6, QTableWidgetItem(f"₹{price:.2f}"))
                self.table_stock.setItem(idx, 7, QTableWidgetItem(str(qty or 0)))
        except Exception as e:
            print("Error loading stock:", e)

    def update_stock_and_price(self):
        barcode = self.txt_barcode.text().strip()
        qty_str = self.txt_qty.text().strip()
        price_str = self.txt_new_price.text().strip()
        
        if not barcode:
            QMessageBox.warning(self, "Error", "Barcode or SKU is required!")
            return
            
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, stock_qty, price_paise FROM product_variants WHERE barcode = ?", (barcode,))
        variant = cursor.fetchone()
        
        if not variant:
            QMessageBox.warning(self, "Not Found", f"No variant found with Barcode: {barcode}")
            return
            
        variant_id, current_qty, current_price = variant
        
        # Calculate Stock In / Out
        qty_change = int(qty_str) if qty_str.isdigit() else 0
        if self.cmb_action.currentText() == "Stock Out (-)":
            new_qty = max(0, current_qty - qty_change)
        else:
            new_qty = current_qty + qty_change
            
        # Variant Specific Price Update
        new_price_paise = current_price
        if price_str:
            try:
                new_price_paise = int(float(price_str) * 100)
            except ValueError:
                pass
                
        cursor.execute("UPDATE product_variants SET stock_qty = ?, price_paise = ? WHERE id = ?", 
                       (new_qty, new_price_paise, variant_id))
        self.conn.commit()
        
        QMessageBox.information(self, "Success", "Stock Qty & Variant Price updated successfully!")
        self.txt_barcode.clear()
        self.txt_qty.clear()
        self.txt_new_price.clear()
        self.load_stock_data()

    def export_to_excel(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Stock Data", "", "CSV Files (*.csv)")
        if not path:
            return
            
        try:
            with open(path, mode='w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                headers = ["Barcode", "Product Name", "Category", "Brand", "Size", "Color", "Price", "Stock Qty"]
                writer.writerow(headers)
                
                for row in range(self.table_stock.rowCount()):
                    row_data = [self.table_stock.item(row, col).text() for col in range(8)]
                    writer.writerow(row_data)
                    
            QMessageBox.information(self, "Export Success", f"Stock exported successfully to:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Export Failed", str(e))

    def import_from_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Stock Data", "", "CSV Files (*.csv)")
        if not path:
            return
            
        try:
            with open(path, mode='r', encoding='utf-8') as file:
                reader = csv.reader(file)
                header = next(reader, None) # Skip header
                
                cursor = self.conn.cursor()
                imported_count = 0
                for row in reader:
                    if len(row) >= 8:
                        barcode, name, cat, brand, size, color, price, qty = row[:8]
                        # Clean price & qty
                        price_val = int(float(price.replace('₹', '').strip()) * 100) if price else 0
                        qty_val = int(qty) if qty.isdigit() else 0
                        
                        cursor.execute("""
                            UPDATE product_variants 
                            SET stock_qty = ?, price_paise = ? 
                            WHERE barcode = ?
                        """, (qty_val, price_val, barcode))
                        imported_count += 1
                        
                self.conn.commit()
                QMessageBox.information(self, "Import Success", f"Successfully imported/updated {imported_count} stock variants!")
                self.load_stock_data()
        except Exception as e:
            QMessageBox.warning(self, "Import Failed", str(e))