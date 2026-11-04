# core/reports.py

def get_gst_summary_report(conn):
    """Calculates total sales and GST collected for reporting."""
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(tax_amount), SUM(total_amount) FROM sales_invoices")
    row = cursor.fetchone()
    return {
        "total_tax": row[0] or 0.0,
        "total_sales": row[1] or 0.0
    }

def get_day_close_reconciliation(conn, date_str):
    """Summarizes day-close transactions by payment mode."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT payment_mode, SUM(total_amount) FROM sales_invoices WHERE DATE(created_at) = ? GROUP BY payment_mode",
        (date_str,)
    )
    rows = cursor.fetchall()
    return {mode: total for mode, total in rows}