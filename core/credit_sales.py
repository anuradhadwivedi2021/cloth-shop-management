# core/credit_sales.py
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Any, List

def check_credit_limit_exceeded(conn: sqlite3.Connection, customer_id: int, new_bill_amount: float) -> Dict[str, Any]:
    """
    Checks if a new bill exceeds customer's credit limit.
    Returns whether override by Owner PIN is required.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT credit_enabled, credit_limit 
        FROM customers 
        WHERE id = ?
    """, (customer_id,))
    row = cursor.fetchone()
    
    if not row or not row[0]: # Credit disabled
        return {"allowed": False, "requires_pin": False, "reason": "Credit sales not enabled for this customer."}
    
    credit_limit = row[1]
    
    # Calculate current outstanding dues
    cursor.execute("""
        SELECT SUM(total_amount - paid_amount) 
        FROM customer_dues_ledger 
        WHERE customer_id = ? AND status != 'PAID'
    """, (customer_id,))
    current_dues = cursor.fetchone()[0] or 0.0
    
    projected_dues = current_dues + new_bill_amount
    
    if projected_dues > credit_limit:
        return {
            "allowed": False,
            "requires_pin": True,
            "current_dues": current_dues,
            "credit_limit": credit_limit,
            "exceeded_by": projected_dues - credit_limit,
            "reason": f"Credit limit of ₹{credit_limit:.2f} exceeded by ₹{projected_dues - credit_limit:.2f}. Owner PIN required."
        }
    
    return {"allowed": True, "requires_pin": False, "current_dues": current_dues}

def get_customer_ageing_report(conn: sqlite3.Connection, customer_id: int) -> Dict[str, float]:
    """
    Calculates 0-30, 31-60, and 60+ days ageing buckets for customer dues.
    """
    cursor = conn.cursor()
    today = datetime.now().date()
    
    cursor.execute("""
        SELECT total_amount - paid_amount, created_at 
        FROM customer_dues_ledger 
        WHERE customer_id = ? AND status != 'PAID'
    """, (customer_id,))
    
    rows = cursor.fetchall()
    
    ageing = {
        "bucket_0_30": 0.0,
        "bucket_31_60": 0.0,
        "bucket_60_plus": 0.0,
        "total_outstanding": 0.0
    }
    
    for due_amount, created_str in rows:
        created_date = datetime.strptime(created_str[:10], "%Y-%m-%d").date()
        days_old = (today - created_date).days
        
        ageing["total_outstanding"] += due_amount
        
        if days_old <= 30:
            ageing["bucket_0_30"] += due_amount
        elif 31 <= days_old <= 60:
            ageing["bucket_31_60"] += due_amount
        else:
            ageing["bucket_60_plus"] += due_amount
            
    return ageing