# core/privacy.py
import json

def export_customer_data(conn, customer_id):
    """Exports all customer personal data in JSON format."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers WHERE id = ?", (customer_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return json.dumps(dict(row), default=str)

def erase_customer_data(conn, customer_id):
    """Anonymizes customer records upon privacy request."""
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE customers SET name = 'ANONYMOUS', phone = '0000000000', email = '' WHERE id = ?",
        (customer_id,)
    )
    conn.commit()
    return True