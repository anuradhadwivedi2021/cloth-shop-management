# ui/products_matrix_view.py
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt

from core.matrix_entry import SELLABLE_UNITS, preview_matrix, save_matrix_product
from core.permissions import PermissionDenied
from core.product_edit import DuplicateProductError

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
        self.cmb_unit.addItems(list(SELLABLE_UNITS))
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
        self.table_preview = QTableWidget(0, 2)
        self.table_preview.setHorizontalHeaderLabels(["Size", "Colour"])
        matrix_layout.addWidget(self.table_preview)
        matrix_layout.addWidget(QLabel("SKU and barcode are generated automatically when you save."))
        
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
        actions.addWidget(btn_cancel)
        actions.addWidget(btn_save)
        
        layout.addLayout(actions)

    def generate_grid_preview(self):
        try:
            combos = preview_matrix(self.txt_sizes.text(), self.txt_colours.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Cloth Shop", str(exc))
            return
        self.table_preview.setRowCount(0)
        for size, colour in combos:
            row = self.table_preview.rowCount()
            self.table_preview.insertRow(row)
            self.table_preview.setItem(row, 0, QTableWidgetItem(size))
            self.table_preview.setItem(row, 1, QTableWidgetItem(colour))

    def _save(self, allow_duplicate):
        return save_matrix_product(
            self.conn, self.user_id,
            name=self.txt_name.text(), category=self.txt_category.text(),
            unit=self.cmb_unit.currentText(), price_text=self.txt_price.text(),
            cost_text=self.txt_cost.text(), sizes_text=self.txt_sizes.text(),
            colours_text=self.txt_colours.text(), allow_duplicate=allow_duplicate,
        )

    def save_matrix_product(self):
        try:
            try:
                result = self._save(False)
            except DuplicateProductError:
                ask = QMessageBox.question(
                    self, "Cloth Shop",
                    "A product with this name already exists. Please review it first.\n\nAdd this as a new product anyway?",
                )
                if ask != QMessageBox.StandardButton.Yes:
                    return
                result = self._save(True)
        except (ValueError, PermissionDenied) as exc:
            QMessageBox.warning(self, "Cloth Shop", str(exc))
            return
        except Exception:
            QMessageBox.critical(self, "Cloth Shop", "The product could not be saved (error E-PRODUCT). Nothing was changed.")
            return
        QMessageBox.information(self, "Cloth Shop", f"Product saved with {len(result['variant_ids'])} variants.")
        self.accept()
