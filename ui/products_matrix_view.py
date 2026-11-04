from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QAbstractItemView
)
from core.products import search_products, get_product_variants


class ProductsMatrixView(QWidget):
    """Products & Stock Management Screen with Matrix Grid and Search (PySide6)"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # Search Bar Header
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search Product / SKU:"))
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Type product name or barcode...")
        self.search_input.returnPressed.connect(self._perform_search)
        search_layout.addWidget(self.search_input)

        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._perform_search)
        search_layout.addWidget(search_btn)

        layout.addLayout(search_layout)

        # Products Table
        self.product_table = QTableWidget()
        self.product_table.setColumnCount(4)
        self.product_table.setHorizontalHeaderLabels(["ID", "Product Name", "Category", "Base Price (₹)"])
        self.product_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.product_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.product_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.product_table.doubleClicked.connect(self._load_variants_matrix)
        layout.addWidget(self.product_table)

        # Matrix Variants Section Label
        layout.addWidget(QLabel("<b>Size x Colour Stock Matrix (Double click a product above)</b>"))

        # Variant Matrix Table
        self.matrix_table = QTableWidget()
        self.matrix_table.setColumnCount(5)
        self.matrix_table.setHorizontalHeaderLabels(["Variant ID", "Size", "Colour", "Stock Qty", "Price (₹)"])
        self.matrix_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.matrix_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.matrix_table)

        # Initial load all products
        self._perform_search()

    def _perform_search(self):
        query = self.search_input.text().strip()
        try:
            products = search_products(query)
        except Exception:
            products = []

        self.product_table.setRowCount(len(products))
        for row_idx, p in enumerate(products):
            self.product_table.setItem(row_idx, 0, QTableWidgetItem(str(p.get("id", ""))))
            self.product_table.setItem(row_idx, 1, QTableWidgetItem(str(p.get("name", ""))))
            self.product_table.setItem(row_idx, 2, QTableWidgetItem(str(p.get("category", ""))))
            self.product_table.setItem(row_idx, 3, QTableWidgetItem(f"{p.get('base_price', 0.0):.2f}"))

    def _load_variants_matrix(self):
        selected_rows = self.product_table.selectionModel().selectedRows()
        if not selected_rows:
            return

        row_idx = selected_rows[0].row()
        product_id = int(self.product_table.item(row_idx, 0).text())

        try:
            variants = get_product_variants(product_id)
        except Exception:
            variants = []

        self.matrix_table.setRowCount(len(variants))
        for row_idx, v in enumerate(variants):
            self.matrix_table.setItem(row_idx, 0, QTableWidgetItem(str(v.get("id", ""))))
            self.matrix_table.setItem(row_idx, 1, QTableWidgetItem(str(v.get("size", ""))))
            self.matrix_table.setItem(row_idx, 2, QTableWidgetItem(str(v.get("colour", ""))))
            
            stock_item = QTableWidgetItem(str(v.get("stock_qty", 0)))
            if v.get("stock_qty", 0) <= 0:
                stock_item.setForeground(Qt.gray)
            self.matrix_table.setItem(row_idx, 3, stock_item)
            
            self.matrix_table.setItem(row_idx, 4, QTableWidgetItem(f"{v.get('price', 0.0):.2f}"))



MatrixProductDialog = ProductsMatrixView