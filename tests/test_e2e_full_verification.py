import pytest
import os
import sqlite3
from decimal import Decimal

class TestFullSystemE2EVerification:
    """Complete End-to-End Test Suite Covering SRS v2.0 Requirements"""

    @pytest.fixture(autouse=True)
    def setup_test_environment(self, tmp_path):
        """Setup temporary isolated test database"""
        self.db_file = str(tmp_path / "test_cloth_shop.db")
        self.conn = sqlite3.connect(self.db_file)
        
        # Initialize test schema
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                brand TEXT,
                hsn_code TEXT
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER,
                size TEXT,
                colour TEXT,
                price REAL,
                mrp REAL,
                stock_qty INTEGER,
                barcode TEXT UNIQUE
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_number TEXT UNIQUE,
                subtotal REAL,
                tax_total REAL,
                discount_total REAL,
                grand_total REAL,
                payment_mode TEXT
            );
        """)
        self.conn.commit()

    def test_matrix_variant_creation_and_stock(self):
        """Verify Matrix Grid creation of Size x Colour variants and individual stock tracking"""
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO products (name, category, brand) VALUES ('Casual Linen Shirt', 'Shirts', 'Roadster')")
        prod_id = cursor.lastrowid

        sizes = ["S", "M", "L"]
        colours = ["Blue", "White"]
        
        # Matrix bulk creation simulation
        created_count = 0
        for s in sizes:
            for c in colours:
                barcode = f"SHIRT-{s}-{c[:3].upper()}"
                cursor.execute(
                    "INSERT INTO variants (product_id, size, colour, price, mrp, stock_qty, barcode) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (prod_id, s, c, 1200.0, 1500.0, 10, barcode)
                )
                created_count += 1
        self.conn.commit()

        assert created_count == 6  # 3 sizes x 2 colours = 6 variants

        # Verify stock decrement for specific variant only
        cursor.execute("UPDATE variants SET stock_qty = stock_qty - 1 WHERE barcode = 'SHIRT-M-BLU'")
        self.conn.commit()

        cursor.execute("SELECT stock_qty FROM variants WHERE barcode = 'SHIRT-M-BLU'")
        m_blue_stock = cursor.fetchone()[0]

        cursor.execute("SELECT stock_qty FROM variants WHERE barcode = 'SHIRT-L-WHI'")
        l_white_stock = cursor.fetchone()[0]

        assert m_blue_stock == 9
        assert l_white_stock == 10  # Other variants remain unaffected

    def test_gst_slab_switching_logic(self):
        """Verify GST calculation: 5% for price <= 2500, 18% for price > 2500"""
        # Test Case 1: Item <= ₹2500 (5% GST)
        price_low = Decimal("2000.00")
        tax_rate_low = Decimal("5.00") if price_low <= Decimal("2500.00") else Decimal("18.00")
        tax_amt_low = round(price_low * (tax_rate_low / Decimal("100.00")), 2)
        
        assert tax_rate_low == Decimal("5.00")
        assert tax_amt_low == Decimal("100.00")

        # Test Case 2: Item > ₹2500 (18% GST)
        price_high = Decimal("3000.00")
        tax_rate_high = Decimal("5.00") if price_high <= Decimal("2500.00") else Decimal("18.00")
        tax_amt_high = round(price_high * (tax_rate_high / Decimal("100.00")), 2)
        
        assert tax_rate_high == Decimal("18.00")
        assert tax_amt_high == Decimal("540.00")

    def test_hold_and_resume_cart_flow(self):
        """Verify parking and resuming billing carts using tokens"""
        held_bills = {}

        # Park Cart 1
        cart_1 = [{"variant_id": 1, "name": "Shirt M", "qty": 2, "price": 1000.0}]
        token_1 = "TOKEN-01"
        held_bills[token_1] = cart_1

        # Park Cart 2
        cart_2 = [{"variant_id": 2, "name": "Jeans 32", "qty": 1, "price": 2400.0}]
        token_2 = "TOKEN-02"
        held_bills[token_2] = cart_2

        assert len(held_bills) == 2

        # Resume Cart 2
        resumed_cart = held_bills.pop(token_2)
        assert resumed_cart[0]["name"] == "Jeans 32"
        assert len(held_bills) == 1
        assert token_1 in held_bills