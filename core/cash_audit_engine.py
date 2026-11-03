# core/cash_audit_engine.py
import sqlite3
from typing import Dict, Any

def get_daily_cash_summary(conn: sqlite3.Connection, target_date: str) -> Dict[str, Any]:
    """
    Calculates expected cash drawer balance for a given date:
    Expected Cash = Opening Balance + Cash Sales/Receipts - Cash Expenses - Cash Refunds
    """
    cursor = conn.cursor()
    
    # 1. Fetch Opening Cash (Default 0 if not set)
    cursor.execute("SELECT value FROM settings WHERE key = 'opening_cash_paise'")
    row = cursor.fetchone()
    opening_paise = int(row[0]) if row else 0
    
    # 2. Cash Inflow from Sales (in INTEGER paise)
    cursor.execute("""
        SELECT COALESCE(SUM(amount_paise), 0) 
        FROM payments 
        WHERE mode = 'cash' AND document_type = 'sale' 
          AND date(created_at) = date(?)
    """, (target_date,))
    cash_in_paise = cursor.fetchone()[0] or 0
    
    # 3. Cash Outflow from Expenses
    cursor.execute("""
        SELECT COALESCE(SUM(amount_paise), 0) 
        FROM payments 
        WHERE mode = 'cash' AND document_type = 'expense' 
          AND date(created_at) = date(?)
    """, (target_date,))
    cash_out_paise = cursor.fetchone()[0] or 0
    
    expected_paise = opening_paise + cash_in_paise - cash_out_paise
    
    return {
        "opening_rs": opening_paise / 100.0,
        "cash_in_rs": cash_in_paise / 100.0,
        "cash_out_rs": cash_out_paise / 100.0,
        "expected_cash_rs": expected_paise / 100.0
    }

def record_cash_closing(
    conn: sqlite3.Connection,
    closing_date: str,
    physical_count_rs: float,
    expected_rs: float,
    notes: str,
    user_id: int
) -> int:
    """Logs day close cash drawer count. Difference is logged without silent manipulation."""
    cursor = conn.cursor()
    diff_paise = int((physical_count_rs - expected_rs) * 100)
    
    cursor.execute("""
        INSERT INTO audit_log (user_id, action, entity, before_json, after_json)
        VALUES (?, 'DAY_CLOSE_CASH', 'CASH_REGISTER', ?, ?)
    """, (
        user_id,
        f'{{"expected_rs": {expected_rs:.2f}}}',
        f'{{"physical_rs": {physical_count_rs:.2f}, "diff_rs": {diff_paise / 100.0:.2f}, "notes": "{notes}"}}'
    ))
    conn.commit()
    return cursor.lastrowid