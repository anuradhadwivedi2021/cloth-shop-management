# core/vouchers_loyalty.py
import sqlite3
from typing import Optional, Dict, Any

def issue_gift_voucher(conn: sqlite3.Connection, code: str, amount: float, customer_id: Optional[int], expiry_date: Optional[str]):
    """
    Issues a new Gift Voucher.
    Note: Income is NOT recognized here. Amount is logged under Liability.
    """
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO gift_vouchers (code, customer_id, initial_amount, current_balance, expiry_date)
        VALUES (?, ?, ?, ?, ?)
    """, (code.strip().upper(), customer_id, amount, amount, expiry_date))
    conn.commit()
    return cursor.lastrowid

def redeem_voucher(conn: sqlite3.Connection, code: str, redeem_amount: float, sale_id: str):
    """
    Redeems a Gift Voucher towards a sale split payment.
    Reduces the Liability balance.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT id, current_balance, status FROM gift_vouchers WHERE code = ?", (code.strip().upper(),))
    row = cursor.fetchone()
    
    if not row:
        raise ValueError("Invalid voucher code.")
    
    v_id, balance, status = row
    if status != 'ACTIVE' or balance <= 0:
        raise ValueError("Voucher is inactive or has zero balance.")
    
    if redeem_amount > balance:
        raise ValueError(f"Redeem amount exceeds available voucher balance of ₹{balance:.2f}")
    
    new_balance = balance - redeem_amount
    new_status = 'FULLY_REDEEMED' if new_balance == 0 else 'ACTIVE'
    
    cursor.execute("""
        UPDATE gift_vouchers 
        SET current_balance = ?, status = ? 
        WHERE id = ?
    """, (new_balance, new_status, v_id))
    
    conn.commit()

def process_store_credit(conn: sqlite3.Connection, customer_id: int, amount: float, tx_type: str, reference_id: str, notes: str = ""):
    """
    Logs Store Credit transaction (Earn/Redeem/Return Reversal).
    """
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO store_credit_ledger (customer_id, amount, tx_type, reference_id, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (customer_id, amount, tx_type, reference_id, notes))
    conn.commit()