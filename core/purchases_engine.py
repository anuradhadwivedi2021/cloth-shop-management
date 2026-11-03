# core/purchases_engine.py
import sqlite3
from typing import Dict, Any, List

def check_duplicate_supplier_invoice(
    conn: sqlite3.Connection, supplier_id: int, invoice_no: str, fin_year: str
) -> bool:
    """Checks if supplier invoice already exists for the same supplier and financial year."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM purchases 
        WHERE supplier_id = ? AND invoice_no = ? AND fin_year = ? AND status != 'cancelled'
    """, (supplier_id, invoice_no.strip(), fin_year))
    return cursor.fetchone()[0] > 0

def create_draft_purchase(
    conn: sqlite3.Connection,
    supplier_id: int,
    invoice_no: str,
    invoice_date: str,
    fin_year: str,
    lines: List[Dict[str, Any]],
    user_id: int,
    freight_paise: int = 0,
    notes: str = ""
) -> int:
    """Creates a Draft purchase invoice. Stock is NOT updated yet."""
    if check_duplicate_supplier_invoice(conn, supplier_id, invoice_no, fin_year):
        raise ValueError(f"Supplier invoice '{invoice_no}' already exists for this financial year!")

    cursor = conn.cursor()
    
    subtotal_paise = sum(line["line_total_paise"] for line in lines)
    tax_paise = sum(line.get("tax_paise", 0) for line in lines)
    total_paise = subtotal_paise + tax_paise + freight_paise
    
    cursor.execute("""
        INSERT INTO purchases (
            supplier_id, invoice_no, invoice_date, fin_year, status,
            subtotal_paise, tax_paise, freight_paise, total_paise, due_paise,
            notes, created_by
        ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)
    """, (
        supplier_id, invoice_no.strip(), invoice_date, fin_year,
        subtotal_paise, tax_paise, freight_paise, total_paise, total_paise,
        notes, user_id
    ))
    
    purchase_id = cursor.lastrowid
    
    for line in lines:
        cursor.execute("""
            INSERT INTO purchase_lines (
                purchase_id, variant_id, qty, unit_cost_paise, tax_rate_percent, tax_paise, line_total_paise
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            purchase_id, line["variant_id"], line["qty_milli"],
            line["unit_cost_paise"], line.get("tax_rate_percent", 0.0),
            line.get("tax_paise", 0), line["line_total_paise"]
        ))
        
    conn.commit()
    return purchase_id

def confirm_and_receive_purchase(conn: sqlite3.Connection, purchase_id: int, user_id: int) -> Dict[str, Any]:
    """
    Confirms a purchase invoice:
    1. Increases stock for each variant in stock_movements (milli-units).
    2. Updates purchase status to 'confirmed'.
    3. Returns variant list and quantities for batch barcode printing.
    """
    cursor = conn.cursor()
    
    cursor.execute("SELECT status, invoice_no FROM purchases WHERE id = ?", (purchase_id,))
    row = cursor.fetchone()
    if not row or row[0] != 'draft':
        raise ValueError("Only draft purchases can be confirmed.")
    
    invoice_no = row[1]
    
    # Fetch lines
    cursor.execute("""
        SELECT variant_id, qty FROM purchase_lines WHERE purchase_id = ?
    """, (purchase_id,))
    lines = cursor.fetchall()
    
    barcode_print_list = []
    
    for variant_id, qty_milli in lines:
        # Fetch current stock balance
        cursor.execute("""
            SELECT COALESCE(SUM(qty_change), 0) 
            FROM stock_movements 
            WHERE variant_id = ? AND bucket = 'sellable'
        """, (variant_id,))
        current_bal = cursor.fetchone()[0] or 0
        new_bal = current_bal + qty_milli
        
        # Insert Stock Movement
        cursor.execute("""
            INSERT INTO stock_movements (
                variant_id, qty_change, bucket, reason, doc_type, doc_id, user_id, balance_after
            ) VALUES (?, ?, 'sellable', ?, 'purchase', ?, ?, ?)
        """, (variant_id, qty_milli, f"Purchase Recv: {invoice_no}", purchase_id, user_id, new_bal))
        
        # Prepare Barcode label data (converting milli-units to piece count)
        barcode_print_list.append({
            "variant_id": variant_id,
            "label_qty": qty_milli // 1000
        })
        
    # Mark confirmed
    cursor.execute("""
        UPDATE purchases 
        SET status = 'confirmed', confirmed_at = datetime('now','localtime')
        WHERE id = ?
    """, (purchase_id,))
    
    conn.commit()
    return {"purchase_id": purchase_id, "labels_to_print": barcode_print_list}