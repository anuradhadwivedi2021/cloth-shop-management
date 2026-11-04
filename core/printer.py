import os
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.pdfgen import canvas

class InvoicePrinter:
    """ReportLab based professional invoice, thermal bill, and barcode label printer."""

    @staticmethod
    def generate_a4_invoice(invoice_data, filename="invoice_a4.pdf"):
        doc = SimpleDocTemplate(filename, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
        story = []
        styles = getSampleStyleSheet()

        # Header
        title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=20, alignment=1, textColor=colors.navy)
        story.append(Paragraph("CLOTH SHOP MANAGEMENT SYSTEM", title_style))
        story.append(Paragraph("<b>Tax Invoice / Cash Memo</b>", ParagraphStyle('Sub', parent=styles['Normal'], alignment=1, fontSize=12)))
        story.append(Spacer(1, 15))

        # Shop & Customer Info
        info_data = [
            [f"<b>Shop:</b> {invoice_data.get('shop_name', 'Cloth Store')}", f"<b>Invoice No:</b> {invoice_data.get('invoice_no', 'INV-001')}"],
            [f"<b>Address:</b> {invoice_data.get('address', 'Pune, Maharashtra')}", f"<b>Date:</b> {invoice_data.get('date', '2026-06-06')}"]
        ]
        info_table = Table(info_data, colWidths=[250, 250])
        info_table.setStyle(TableStyle([
            ('TEXTCOLOR', (0,0), (-1,-1), colors.black),
            ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
            ('FONTSIZE', (0,0), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ]))
        story.append(info_table)
        story.append(Spacer(1, 15))

        # Items Table
        table_data = [["#", "Item Name", "Size", "Colour", "Qty", "Price (₹)", "Total (₹)"]]
        for idx, item in enumerate(invoice_data.get("items", []), 1):
            table_data.append([
                str(idx),
                item.get("name", ""),
                str(item.get("size", "")),
                str(item.get("colour", "")),
                str(item.get("qty", 1)),
                f"{item.get('price', 0):.2f}",
                f"{item.get('qty', 1) * item.get('price', 0):.2f}"
            ])

        item_table = Table(table_data, colWidths=[30, 180, 50, 60, 40, 70, 70])
        item_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.navy),
            ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0,0), (-1,0), 6),
            ('BACKGROUND', (0,1), (-1,-1), colors.whitesmoke),
            ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
            ('FONTSIZE', (0,0), (-1,-1), 9),
        ]))
        story.append(item_table)
        story.append(Spacer(1, 15))

        # Totals
        grand_total = sum(i.get('qty', 1) * i.get('price', 0) for i in invoice_data.get("items", []))
        summary_data = [
            ["", f"Grand Total: ₹{grand_total:.2f}"]
        ]
        sum_table = Table(summary_data, colWidths=[300, 200])
        sum_table.setStyle(TableStyle([
            ('ALIGN', (1,0), (1,0), 'RIGHT'),
            ('FONTNAME', (1,0), (1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (1,0), (1,0), 12),
        ]))
        story.append(sum_table)

        if invoice_data.get("is_duplicate", False):
            story.append(Spacer(1, 20))
            dup_style = ParagraphStyle('Dup', parent=styles['Normal'], alignment=1, fontSize=14, textColor=colors.red)
            story.append(Paragraph("<b>*** DUPLICATE COPY ***</b>", dup_style))

        doc.build(story)
        return filename

    @staticmethod
    def generate_barcode_label(sku, price, filename="barcode_label.pdf"):
        # Custom small width/height for thermal barcode sticker (e.g., 50mm x 25mm in points)
        doc = SimpleDocTemplate(filename, pagesize=(141, 70), rightMargin=5, leftMargin=5, topMargin=5, bottomMargin=5)
        story = []
        styles = getSampleStyleSheet()

        story.append(Paragraph(f"<b>CLOTH STORE</b>", ParagraphStyle('S1', fontSize=8, alignment=1)))
        story.append(Paragraph(f"SKU: {sku}", ParagraphStyle('S2', fontSize=7, alignment=1)))
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"<b>Price: ₹{price:.2f}</b>", ParagraphStyle('S3', fontSize=9, alignment=1, textColor=colors.black)))

        doc.build(story)
        return filename