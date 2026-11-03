# core/matrix_controller.py
import sqlite3
import re
from typing import List, Dict, Any

def generate_sku(product_code: str, size: str, colour: str) -> str:
    """Generates unique clean SKU from Product Code, Size, and Colour."""
    clean_p = re.sub(r'[^A-Z0-9]', '', product_code.upper())
    clean_s = re.sub(r'[^A-Z0-9]', '', size.upper())
    clean_c = re.sub(r'[^A-Z0-9]', '', colour.upper())
    return f"{clean_p}-{clean_s}-{clean_c}"

def generate_ean13_barcode(product_id: int, index: int) -> str:
    """Generates a standard 13-digit internal barcode with checksum."""
    prefix = "200"
    payload = f"{product_id:05d}{index:04d}"
    raw = prefix + payload
    
    odd_sum = sum(int(raw[i]) for i in range(0, 12, 2))
    even_sum = sum(int(raw[i]) for i in range(1, 12, 2))
    total = odd_sum + (even_sum * 3)
    check_digit = (10 - (total % 10)) % 10
    return f"{raw}{check_digit}"

def create_product_with_matrix(
    conn: sqlite3.Connection,
    product_data: Dict[str, Any],
    sizes: List[str],
    colours: List[str],
    user_id: int
) -> int:
    """
    Saves Product and all Size x Colour Matrix Variants in a single transaction.
    All money amounts are converted to integer paise, and quantities to integer milli-units.
    """
    cursor = conn.cursor()
    
    # 1. Insert Parent Product
    cursor.execute("""
        INSERT INTO products (
            name, category, brand, hsn, material, season, description,
            gst_rate_percent, mrp_paise, default_cost_paise, default_price_paise,
            unit, reorder_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        product_data["name"],
        product_data.get("category"),
        product_data.get("brand"),
        product_data.get("hsn"),
        product_data.get("material"),
        product_data.get("season"),
        product_data.get("description"),
        product_data.get("gst_rate_percent", 5.0),
        int(product_data.get("mrp", 0) * 100),
        int(product_data.get("cost_price", 0) * 100),
        int(product_data.get("selling_price", 0) * 100),
        product_data.get("unit", "piece"),
        product_data.get("reorder_level", 0)
    ))
    
    product_id = cursor.lastrowid
    p_code = product_data.get("code") or f"PRD{product_id}"
    
    # 2. Bulk Generate Variants in Grid
    variant_idx = 1
    for colour in colours:
        c_clean = colour.strip()
        for size in sizes:
            s_clean = size.strip()
            sku = generate_sku(p_code, s_clean, c_clean)
            barcode = generate_ean13_barcode(product_id, variant_idx)
            
            cursor.execute("""
                INSERT INTO variants (
                    product_id, size, colour, sku, barcode,
                    min_cut_milli
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                product_id,
                s_clean,
                c_clean,
                sku,
                barcode,
                int(product_data.get("min_cut_length", 0) * 1000)
            ))
            
            variant_id = cursor.lastrowid
            
            # Initial stock entry if provided (in milli-units)
            initial_qty = product_data.get("initial_stock", 0)
            if initial_qty > 0:
                milli_qty = int(initial_qty * 1000)
                cursor.execute("""
                    INSERT INTO stock_movements (
                        variant_id, qty_change, bucket, reason, user_id, balance_after
                    ) VALUES (?, ?, 'sellable', 'Initial Opening Stock', ?, ?)
                """, (variant_id, milli_qty, user_id, milli_qty))
                
            variant_idx += 1
            
    conn.commit()
    return product_id