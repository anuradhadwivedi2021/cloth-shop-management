# core/seasonal_commission.py
import sqlite3
from typing import Dict, Any, List

def calculate_salesperson_commission(conn: sqlite3.Connection, user_id: int, start_date: str, end_date: str, commission_percent: float = 2.0) -> Dict[str, Any]:
    """Calculates salesperson commission based on completed net sales volume."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(SUM(total_paise), 0), COUNT(id)
        FROM sales
        WHERE salesperson_id = ? AND date(created_at) BETWEEN date(?) AND date(?)
          AND status = 'completed'
    """, (user_id, start_date, end_date))
    
    total_sales_paise, bill_count = cursor.fetchone()
    total_sales_rs = total_sales_paise / 100.0
    commission_rs = (total_sales_rs * commission_percent) / 100.0
    
    return {
        "salesperson_id": user_id,
        "total_sales_rs": total_sales_rs,
        "bill_count": bill_count,
        "commission_percent": commission_percent,
        "commission_rs": commission_rs
    }