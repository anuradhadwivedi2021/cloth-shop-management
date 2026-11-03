# core/dashboard_backup_engine.py
import sqlite3
from datetime import datetime
from typing import Dict, Any, List

def get_dashboard_kpi_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Fetches key performance indicators for the main dashboard screen."""
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 1. Today's Sales Total (Paise -> Rs)
    cursor.execute("""
        SELECT COALESCE(SUM(total_paise), 0), COUNT(id) 
        FROM sales 
        WHERE date(created_at) = date(?) AND status = 'completed'
    """, (today_str,))
    row_sales = cursor.fetchone()
    today_sales_rs = (row_sales[0] or 0) / 100.0
    bill_count = row_sales[1] or 0
    
    # 2. Low Stock Variants Count (Milli-units < Reorder Level)
    cursor.execute("""
        SELECT COUNT(v.id) 
        FROM variants v 
        JOIN variant_stock s ON s.variant_id = v.id 
        WHERE s.sellable_qty < COALESCE(v.reorder_level * 1000, 0)
    """)
    low_stock_count = cursor.fetchone()[0] or 0
    
    # 3. Customer Total Outstanding Dues
    cursor.execute("""
        SELECT COALESCE(SUM(total_amount_paise - paid_amount_paise), 0) 
        FROM customer_dues_ledger 
        WHERE status != 'PAID'
    """)
    total_dues_rs = (cursor.fetchone()[0] or 0) / 100.0
    
    return {
        "today_sales_rs": today_sales_rs,
        "bill_count": bill_count,
        "low_stock_count": low_stock_count,
        "total_dues_rs": total_dues_rs
    }

def get_gstr1_summary_report(conn: sqlite3.Connection, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Generates GSTR-1 matching sales tax summary by HSN and GST Rate."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            sl.hsn,
            sl.tax_rate_percent,
            SUM(sl.taxable_paise) / 100.0 AS taxable_amt,
            SUM(sl.tax_paise) / 100.0 AS total_tax,
            SUM(sl.line_total_paise) / 100.0 AS total_val
        FROM sale_lines sl
        JOIN sales s ON sl.sale_id = s.id
        WHERE date(s.created_at) BETWEEN date(?) AND date(?)
          AND s.status = 'completed'
        GROUP BY sl.hsn, sl.tax_rate_percent
    """, (start_date, end_date))
    
    report = []
    for row in cursor.fetchall():
        report.append({
            "hsn": row[0] or "N/A",
            "rate_percent": row[1],
            "taxable_amt_rs": row[2],
            "tax_amt_rs": row[3],
            "total_val_rs": row[4]
        })
    return report

