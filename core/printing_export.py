# core/printing_export.py
import csv
from typing import Dict, Any, List

def format_escpos_thermal_receipt(sale_data: Dict[str, Any], is_reprint: bool = False) -> str:
    """Formats bill receipt text for 58mm/80mm ESC/POS thermal printers."""
    lines = []
    lines.append("=" * 32)
    lines.append(f"     {sale_data.get('shop_name', 'CLOTH SHOP')}     ")
    lines.append("=" * 32)
    
    if is_reprint:
        lines.append("*** DUPLICATE COPY ***")
        
    lines.append(f"Invoice No: {sale_data['invoice_no']}")
    lines.append(f"Date: {sale_data['created_at']}")
    lines.append("-" * 32)
    
    for item in sale_data['items']:
        lines.append(f"{item['name']} ({item['size']}/{item['colour']})")
        lines.append(f"  {item['qty']} x ₹{item['price']:.2f} = ₹{item['total']:.2f}")
        
    lines.append("-" * 32)
    lines.append(f"Subtotal: ₹{sale_data['subtotal']:.2f}")
    lines.append(f"GST Tax: ₹{sale_data['tax']:.2f}")
    lines.append(f"GRAND TOTAL: ₹{sale_data['grand_total']:.2f}")
    lines.append("=" * 32)
    lines.append("   Thank You! Visit Again!   ")
    
    return "\n".join(lines)

def export_gstr1_excel_csv(filepath: str, rows: List[Dict[str, Any]]):
    """Exports GSTR-1 summary to CSV format for accountant upload."""
    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["HSN Code", "GST Rate %", "Taxable Amount (Rs)", "IGST/CGST/SGST (Rs)", "Total Invoice Value (Rs)"])
        for r in rows:
            writer.writerow([r["hsn"], r["rate_percent"], r["taxable_amt_rs"], r["tax_amt_rs"], r["total_val_rs"]])