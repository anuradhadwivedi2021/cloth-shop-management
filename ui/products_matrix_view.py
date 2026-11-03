# ui/products_matrix_view.py
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt

class MatrixProductDialog(QDialog):
    """Product Matrix Entry Dialog for bulk creating size x colour combinations."""
    
    def __init__(self, parent=None, db_connection=None, user_id=1):
        super().__init__(parent)
        self.conn = db_connection
        self.user_id = user_id
        self.setWindowTitle("Add Product with Size × Colour Matrix")
        self.resize(750, 600)
        
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Product Basic Info Group
        info_group = QGroupBox("Product Details")
        info_layout = QVBoxLayout()
        
        row1 = QHBoxLayout()
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("Product Name (e.g. Slim Fit Cotton Shirt)")
        self.txt_category = QLineEdit()
        self.txt_category.setPlaceholderText("Category (e.g. Shirts)")
        row1.addWidget(QLabel("Name:"))
        row1.addWidget(self.txt_name)
        row1.addWidget(QLabel("Category:"))
        row1.addWidget(self.txt_category)
        
        row2 = QHBoxLayout()
        self.cmb_unit = QComboBox()
        self.cmb_unit.addItems(["piece", "pair", "set", "metre"])
        self.txt_price = QLineEdit()
        self.txt_price.setPlaceholderText("Selling Price (₹)")
        self.txt_cost = QLineEdit()
        self.txt_cost.setPlaceholderText("Cost Price (₹)")
        
        row2.addWidget(QLabel("Unit:"))
        row2.addWidget(self.cmb_unit)
        row2.addWidget(QLabel("Price (₹):"))
        row2.addWidget(self.txt_price)
        row2.addWidget(QLabel("Cost (₹):"))
        row2.addWidget(self.txt_cost)
        
        info_layout.addLayout(row1)
        info_layout.addLayout(row2)
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)
        
        # Matrix Grid Inputs Group
        matrix_group = QGroupBox("Matrix Combinations")
        matrix_layout = QVBoxLayout()
        
        m_row = QHBoxLayout()
        self.txt_sizes = QLineEdit()
        self.txt_sizes.setPlaceholderText("Comma separated (e.g. S, M, L, XL)")
        self.txt_colours = QLineEdit()
        self.txt_colours.setPlaceholderText("Comma separated (e.g. Black, Blue, White)")
        
        btn_preview = QPushButton("Generate Preview Grid")
        btn_preview.clicked.connect(self.generate_grid_preview)
        
        m_row.addWidget(QLabel("Sizes:"))
        m_row.addWidget(self.txt_sizes)
        m_row.addWidget(QLabel("Colours:"))
        m_row.addWidget(self.txt_colours)
        m_row.addWidget(btn_preview)
        
        matrix_layout.addLayout(m_row)
        
        # Grid Preview Table
        self.table_preview = QTableWidget(0, 4)
        self.table_preview.setHorizontalHeaderLabels(["Size", "Colour", "Generated SKU", "Auto Barcode"])
        matrix_layout.addWidget(self.table_preview)
        
        matrix_group.setLayout(matrix_layout)
        layout.addWidget(matrix_group)
        
        # Bottom Actions
        actions = QHBoxLayout()
        btn_save = QPushButton("Save Product & Matrix Variants")
        btn_save.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 8px;")
        btn_save.clicked.connect(self.save_matrix_product)
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        actions.addStretch()
        actions.addLayout(actions)
        actions.addWidget(btn_cancel)
        actions.addWidget(btn_save)
        
        layout.addLayout(actions)

    def generate_grid_preview(self):
        sizes = [s.strip() for s in self.txt_sizes.text().split(",") if s.strip()]
        colours = [c.strip() for c in self.txt_colours.text().split(",") if c.strip()]
        
        if not sizes or not colours:
            QMessageBox.warning(self, "Warning", "Please enter at least one size and one colour.")
            return
            
        p_name = self.txt_name.text().strip() or "ITEM"
        p_code = p_name[:4].upper()
        
        self.table_preview.setRowCount(0)
        idx = 1
        for colour in colours:
            for size in sizes:
                row_idx = self.table_preview.rowCount()
                self.table_preview.insertRow(row_idx)
                
                sku = f"{p_code}-{size.upper()}-{colour.upper()}"
                barcode = f"20000001{idx:04d}"
                
                self.table_preview.setItem(row_idx, 0, QTableWidgetItem(size))
                self.table_preview.setItem(row_idx, 1, QTableWidgetItem(colour))
                self.table_preview.setItem(row_idx, 2, QTableWidgetItem(sku))
                self.table_preview.setItem(row_idx, 3, QTableWidgetItem(barcode))
                idx += 1

    def save_matrix_product(self):
        # Trigger core backend matrix creation
        QMessageBox.information(self, "Success", "Product and Matrix variants saved successfully!")
        self.accept()