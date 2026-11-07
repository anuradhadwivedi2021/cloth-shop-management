"""Save a report as a CSV, Excel (.xlsx) or PDF file.

CSV uses only Python's built-in csv module. Excel needs openpyxl and PDF needs reportlab (both in requirements.txt).

Rules (the same for all three formats):
  * Needs the 'export_reports' permission, checked here and not only on the screen.
  * Amounts stored as paise are written as rupees: a column 'total_paise' becomes 'total (Rs)'
    and 123450 becomes '1234.50' (whole-number arithmetic, never float).
  * A text cell that starts with = + - @ is written with a leading ' so Excel never runs it as a formula.
  * The file appears under its real name only when it is complete, and the export is written to the
    audit log in the same transaction. If either fails, no half file is left behind.
  * Excel and PDF files hold the WHOLE report (the figures and every table); a CSV holds one table.
  * Excel keeps money and quantities as real numbers (so they can be added up). PDF is written with money as
    "Rs 1,23,456.78"; Hindi/Marathi text needs a Devanagari font on the computer (see _pdf_font), otherwise
    those letters are shown as "?".
  * Report rows may hold personal data (customer names, phone numbers): hand such files only to people allowed to see them.
"""

import csv
import json
import os
import re
from datetime import datetime
from decimal import Decimal

from core.audit import log_event
from core.money import format_rupees
from core.permissions import require_permission
from db.connection import transaction

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _rupees(paise) -> str:
    sign = "-" if paise < 0 else ""
    whole, part = divmod(abs(paise), 100)
    return f"{sign}{whole}.{part:02d}"


def _header(name: str) -> str:
    return f"{name[:-6]} (Rs)" if name.endswith("_paise") else name


def _cell(column: str, value):
    if value is None:
        return ""
    if column.endswith("_paise") and isinstance(value, int) and not isinstance(value, bool):
        return _rupees(value)
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def export_report_csv(conn, path, report, *, user_id, report_name, columns=None) -> str:
    """Write a report to `path`. `report` is a list of row dicts or a report dict with a 'rows' list.

    `columns` chooses and orders the columns (default: the keys of the first row).
    Returns the path.
    """
    require_permission(conn, user_id, "export_reports", "export reports")
    # most reports keep their lines under "rows"; the day book keeps them under "entries"
    rows = (report.get("rows") if "rows" in report else report.get("entries")) if isinstance(report, dict) else report
    if not isinstance(rows, (list, tuple)):
        raise ValueError("There is nothing to export for this report.")
    columns = list(columns) if columns else (list(rows[0].keys()) if rows else [])
    if not columns:
        raise ValueError("There is nothing to export for this report.")

    path = os.fspath(path)
    temp = f"{path}.part"
    try:
        with open(temp, "w", encoding="utf-8-sig", newline="") as handle:  # utf-8-sig: Excel shows Hindi/Marathi right
            writer = csv.writer(handle)
            writer.writerow([_header(c) for c in columns])
            for row in rows:
                writer.writerow([_cell(c, row.get(c)) for c in columns])
        with transaction(conn):
            log_event(conn, "report_exported", user_id=user_id, entity="report", entity_id=None,
                      after={"report": report_name, "rows": len(rows), "file": os.path.basename(path)})
            os.replace(temp, path)
    except Exception:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    return path


# ====================================================================== Excel and PDF

FORMATS = {"xlsx": ".xlsx", "pdf": ".pdf"}
MAX_PDF_ROWS = 5000
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_ACRONYMS = {"gst": "GST", "cgst": "CGST", "sgst": "SGST", "igst": "IGST", "gstin": "GSTIN", "hsn": "HSN",
             "b2b": "B2B", "b2c": "B2C", "sku": "SKU", "upi": "UPI", "mrp": "MRP", "id": "ID"}


def _nice_header(name) -> str:
    """'taxable_paise' -> 'Taxable (Rs)', 'rate_percent' -> 'Rate percent'."""
    text = str(name)
    money = text.endswith("_paise")
    if money:
        text = text[:-6]
    words = text.replace("_", " ").split()
    text = " ".join(_ACRONYMS.get(w.lower(), w) for w in words)
    text = text[:1].upper() + text[1:]
    return f"{text} (Rs)" if money else text


def _columns(rows) -> list:
    seen = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


def _clean_tables(tables) -> list:
    """[(title, rows)] with the empty tables dropped; refuses when nothing is left."""
    kept = [(str(title or ""), [r for r in rows if isinstance(r, dict)]) for title, rows in tables]
    kept = [(t, rows) for t, rows in kept if rows]
    if not kept:
        raise ValueError("There is nothing to export for this report.")
    return kept


def _value(column, value):
    """A cell for Excel: money as exact rupees, numbers as numbers, dates as DD/MM/YYYY, text guarded."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        if str(column).endswith("_paise"):
            return (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    text = str(value)
    if _DATE_ONLY.match(text):
        text = f"{text[8:10]}/{text[5:7]}/{text[0:4]}"
    return "'" + text if text.startswith(_FORMULA_START) else text


def _shown(column, value, money_symbol="Rs ") -> str:
    """A cell for the PDF: the same text as the screen, with 'Rs' because the standard PDF font has no rupee sign."""
    cell = _value(column, value)
    if cell is None:
        return ""
    if isinstance(cell, Decimal):
        return format_rupees(int(value), symbol=money_symbol)
    if isinstance(cell, float):
        return f"{cell:g}"
    return str(cell)


def _sheet_name(title: str, used: set) -> str:
    cleaned = "".join("-" if ch in "[]:*?/\\" else ch for ch in title).strip() or "Report"
    cleaned = cleaned[:31]
    base, n = cleaned, 2
    while cleaned.lower() in used:
        suffix = f" ({n})"
        cleaned = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(cleaned.lower())
    return cleaned


def _write_xlsx(tables, temp, report_name, subtitle, notes) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    book.remove(book.active)
    used = set()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2F4F6F")
    for index, (title, rows) in enumerate(tables):
        sheet = book.create_sheet(_sheet_name(_nice_header(title) if title else report_name, used))
        sheet.append([report_name])
        sheet["A1"].font = Font(bold=True, size=14)
        sheet.append([subtitle] if subtitle else [])
        sheet.append([])
        columns = _columns(rows)
        sheet.append([_nice_header(c) for c in columns])
        header_row = sheet.max_row
        for cell in sheet[header_row]:
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row in rows:
            sheet.append([_value(c, row.get(c)) for c in columns])
            for col, c in enumerate(columns, start=1):
                cell = sheet.cell(row=sheet.max_row, column=col)
                if isinstance(cell.value, Decimal):
                    cell.number_format = "#,##0.00"
                if isinstance(cell.value, (Decimal, int, float)):
                    cell.alignment = Alignment(horizontal="right")
        if index == len(tables) - 1 and notes:
            sheet.append([])
            for note in notes:
                sheet.append([note])
        for col, c in enumerate(columns, start=1):
            longest = max([len(_nice_header(c))] + [len(_shown(c, r.get(c), "")) for r in rows[:300]])
            sheet.column_dimensions[get_column_letter(col)].width = min(max(10, longest + 2), 48)
        sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
    book.save(temp)


_DEVANAGARI_FONTS = (
    r"C:\Windows\Fonts\Nirmala.ttf", r"C:\Windows\Fonts\mangal.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)


def _pdf_font() -> str | None:
    """Registers and returns the name of a font that can draw Devanagari, or None when this computer has none."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _DEVANAGARI_FONTS:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("ShopDevanagari", path))
                return "ShopDevanagari"
            except Exception:  # noqa: BLE001 - a broken font file just means we fall back to the standard font
                continue
    return None


def _write_pdf(tables, temp, report_name, subtitle, notes, shop_name) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    total_rows = sum(len(rows) for _t, rows in tables)
    if total_rows > MAX_PDF_ROWS:
        raise ValueError(f"This report has {total_rows} rows, too many for a PDF. Save it as Excel or CSV instead.")
    font = _pdf_font()
    base_font = font or "Helvetica"

    def text(value) -> str:
        value = str(value)
        return escape(value if font else value.encode("latin-1", "replace").decode("latin-1"))

    widest = max(len(_columns(rows)) for _t, rows in tables)
    size = landscape(A4) if widest > 6 else A4
    point = 8 if widest <= 8 else 6.8 if widest <= 12 else 5.8
    styles = getSampleStyleSheet()
    for name in ("Title", "Heading3", "Normal"):
        styles[name].fontName = base_font if font else styles[name].fontName
    cell_style = ParagraphStyle("cell", parent=styles["BodyText"], fontName=base_font, fontSize=point,
                                leading=point + 2)
    small = ParagraphStyle("small", parent=cell_style, fontSize=8, leading=10)
    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(base_font, 7.5)
        canvas.drawString(12 * mm, 8 * mm, text(f"{shop_name}   Generated {stamp}".strip()))
        canvas.drawRightString(size[0] - 12 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    story = [Paragraph(text(report_name), styles["Title"])]
    if subtitle:
        story.append(Paragraph(text(subtitle), styles["Normal"]))
    story.append(Spacer(1, 6))
    usable = size[0] - 24 * mm
    bold = (lambda t: t) if font else (lambda t: f"<b>{t}</b>")
    for title, rows in tables:
        if title and title != report_name:
            story.append(Paragraph(text(_nice_header(title)), styles["Heading3"]))
        columns = _columns(rows)
        data = [[Paragraph(bold(text(_nice_header(c))), cell_style) for c in columns]]
        data += [[Paragraph(text(_shown(c, r.get(c))), cell_style) for c in columns] for r in rows]
        weights = [min(max([len(_nice_header(c))] + [len(_shown(c, r.get(c))) for r in rows[:200]]), 32) + 2
                   for c in columns]
        scale = usable / sum(weights)
        table = Table(data, colWidths=[w * scale for w in weights], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F0")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9AA8B5")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FA")]),
        ]))
        story += [table, Spacer(1, 8)]
    for note in notes:
        story.append(Paragraph(text(note), small))
    SimpleDocTemplate(temp, pagesize=size, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                      bottomMargin=14 * mm, title=report_name).build(story, onFirstPage=footer, onLaterPages=footer)


def export_report_file(conn, path, tables, *, fmt, user_id, report_name, subtitle="", notes=(), shop_name="") -> str:
    """Write a whole report as 'xlsx' or 'pdf'. `tables` is [(title, rows)] where rows are dicts (money in *_paise).

    Same rules as export_report_csv: needs "export_reports", the file appears only when complete, and the export
    is audited in the same transaction. Returns the path.
    """
    if fmt not in FORMATS:
        raise ValueError("Format must be xlsx or pdf.")
    require_permission(conn, user_id, "export_reports", "export reports")
    tables = _clean_tables(tables)
    notes = [str(n) for n in notes if n]
    path = os.fspath(path)
    if not path.lower().endswith(FORMATS[fmt]):
        path += FORMATS[fmt]
    temp = f"{path}.part"
    try:
        if fmt == "xlsx":
            _write_xlsx(tables, temp, report_name, subtitle, notes)
        else:
            _write_pdf(tables, temp, report_name, subtitle, notes, shop_name)
        with transaction(conn):
            log_event(conn, "report_exported", user_id=user_id, entity="report", entity_id=None,
                      after={"report": report_name, "format": fmt, "rows": sum(len(r) for _t, r in tables),
                             "file": os.path.basename(path)})
            os.replace(temp, path)
    except Exception:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    return path
