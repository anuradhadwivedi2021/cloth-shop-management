"""Printing: the one place screens call to print.

Connected now:      print_bill (also reprints), save_bill_pdf, print_labels, save_labels_pdf
Still "not connected yet" (own steps): print_credit_note, print_receipt

What the bill says is decided in core/bill_document.py (tested, no screens). This file only hands
that to a printer or saves it as a PDF, using Qt's own text engine so Hindi / Marathi names print.

Rules for callers:
  * Printing happens AFTER the document is saved. A failed print never undoes or repeats a sale:
    show result.message and let the user press Reprint.
  * Reprints and saved PDF copies are marked "Duplicate copy" and logged. They never create a sale.
  * Always check result.ok; never tell the user "printed" when it is False.
  * Paper (A4, A5, 58 mm or 80 mm roll) and the language on the bill come from Settings.
"""

import logging
import os
from dataclasses import dataclass

from core.bill_document import bill_file_name, bill_html, build_bill
from ui.i18n import tr

log = logging.getLogger("clothshop")

_PX_PER_MM = 96 / 25.4  # Qt text documents are laid out at 96 dpi before they are scaled to the printer


@dataclass(frozen=True)
class PrintResult:
    ok: bool
    message: str
    path: str | None = None  # a saved PDF/image, when the result is a file


def _not_connected() -> PrintResult:
    return PrintResult(False, tr("Printing is not connected yet. The document is saved; it can be printed later."))


# ---------------------------------------------------------------- the two Qt steps (replaced in tests)

def _page_for(bill, html):
    """Build the text document and the page size (mm) for this bill's paper.

    A4 / A5 use the paper's fixed size. A roll printer gets a page exactly as long as the bill,
    so no paper is wasted and the cutter fires after the last line.
    """
    from PySide6.QtCore import QSizeF
    from PySide6.QtGui import QTextDocument

    spec = bill["paper"]
    printable_mm = spec["width_mm"] - 2 * spec["margin_mm"]
    doc = QTextDocument()
    doc.setHtml(html)
    doc.setTextWidth(printable_mm * _PX_PER_MM)
    if spec["roll"]:
        height_mm = doc.size().height() / _PX_PER_MM + 2 * spec["margin_mm"] + 6
        height_mm = max(height_mm, 60.0)
    else:
        height_mm = float(spec["height_mm"])
    return doc, QSizeF(spec["width_mm"], height_mm)


def _setup_printer(printer, bill, size_mm):
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize

    margin = bill["paper"]["margin_mm"]
    layout = QPageLayout(
        QPageSize(size_mm, QPageSize.Millimeter), QPageLayout.Portrait,
        QMarginsF(margin, margin, margin, margin), QPageLayout.Millimeter,
    )
    printer.setPageLayout(layout)


def _print_html(bill, html) -> str:
    """Send the bill to the computer's default printer. Returns the printer's name. Raises RuntimeError if none."""
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

    info = QPrinterInfo.defaultPrinter()
    if info.isNull():
        raise RuntimeError("no printer")
    doc, size = _page_for(bill, html)
    printer = QPrinter(info, QPrinter.HighResolution)
    _setup_printer(printer, bill, size)
    doc.print_(printer)
    return info.printerName()


def _write_pdf(bill, html, path) -> None:
    """Save the bill as a PDF file (same layout as the printed copy)."""
    from PySide6.QtGui import QPdfWriter

    doc, size = _page_for(bill, html)
    writer = QPdfWriter(path)      # writes the file directly; no printer or Windows print system involved
    writer.setResolution(1200)     # the same sharpness QPrinter.HighResolution gave
    _setup_printer(writer, bill, size)
    doc.print_(writer)


# ---------------------------------------------------------------- what the screens call

def print_bill(conn, session, sale_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print a saved bill (tax invoice, bill of supply or simple bill, by its own type).

    duplicate=True is a reprint: the copy says "Duplicate copy" and the reprint is logged.
    """
    try:
        if duplicate:
            session.touch()  # a locked screen cannot reprint
        bill = build_bill(conn, sale_id, user_id=session.user_id, duplicate=duplicate)
    except ValueError:
        return PrintResult(False, tr("The bill could not be found."))
    except Exception:  # noqa: BLE001 - e.g. a locked session; the bill itself is already saved
        log.exception("E-PRINT-BILL: could not prepare bill %s", sale_id)
        return PrintResult(False, tr("The printer reported a problem."))

    try:
        printer_name = _print_html(bill, bill_html(bill))
    except RuntimeError:
        return PrintResult(False, tr("No printer was found on this computer. The bill is saved; connect a printer and reprint it."))
    except Exception:  # noqa: BLE001 - a printer problem must never break the sale screen
        log.exception("E-PRINT-BILL: printing bill %s failed", sale_id)
        return PrintResult(False, tr("The printer reported a problem."))
    return PrintResult(True, tr("Bill sent to the printer ({printer}).", printer=printer_name))


def save_bill_pdf(conn, session, sale_id, path) -> PrintResult:
    """Save a saved bill as a PDF the owner can share by hand.

    `path` may be a file name or a folder. For a folder the file is named after the bill number.
    The copy says "Duplicate copy" and is logged, like any reprint.
    """
    try:
        session.touch()
        bill = build_bill(conn, sale_id, user_id=session.user_id, duplicate=True)
    except ValueError:
        return PrintResult(False, tr("The bill could not be found."))
    except Exception:  # noqa: BLE001
        log.exception("E-PRINT-PDF: could not prepare bill %s", sale_id)
        return PrintResult(False, tr("The PDF could not be saved."))

    target = str(path)
    if os.path.isdir(target):
        target = os.path.join(target, bill_file_name(bill))
    elif not target.lower().endswith(".pdf"):
        target += ".pdf"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        _write_pdf(bill, bill_html(bill), target)
        if not os.path.exists(target) or os.path.getsize(target) == 0:
            raise OSError("the PDF file was not written")
    except Exception:  # noqa: BLE001
        log.exception("E-PRINT-PDF: saving bill %s as PDF failed", sale_id)
        return PrintResult(False, tr("The PDF could not be saved."))
    return PrintResult(True, tr("Bill saved as a PDF: {path}", path=target), target)


def _estimate_document(conn, session, estimate_id):
    """(document, None) or (None, PrintResult) for an estimate; the estimate itself is never changed."""
    from core.estimate_document import build_estimate_document

    try:
        session.touch()
        return build_estimate_document(conn, session.user_id, estimate_id), None
    except ValueError:
        return None, PrintResult(False, tr("The estimate could not be found."))
    except Exception:  # noqa: BLE001 - e.g. a locked session
        log.exception("E-PRINT-ESTIMATE: could not prepare estimate %s", estimate_id)
        return None, PrintResult(False, tr("The printer reported a problem."))


def print_estimate(conn, session, estimate_id, *, parent=None) -> PrintResult:
    """Print an estimate on the bill printer. It is a price quote, never a tax invoice."""
    document, failed = _estimate_document(conn, session, estimate_id)
    if failed:
        return failed
    try:
        printer_name = _print_html(document, document["html"])
    except RuntimeError:
        return PrintResult(False, tr("No printer was found on this computer. The estimate is saved; connect a printer and print it again."))
    except Exception:  # noqa: BLE001
        log.exception("E-PRINT-ESTIMATE: printing estimate %s failed", estimate_id)
        return PrintResult(False, tr("The printer reported a problem."))
    return PrintResult(True, tr("Estimate sent to the printer ({printer}).", printer=printer_name))


def save_estimate_pdf(conn, session, estimate_id, path) -> PrintResult:
    """Save an estimate as a PDF to share. `path` may be a file name or a folder."""
    document, failed = _estimate_document(conn, session, estimate_id)
    if failed:
        return failed
    target = str(path)
    if os.path.isdir(target):
        target = os.path.join(target, document["file_name"])
    elif not target.lower().endswith(".pdf"):
        target += ".pdf"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        _write_pdf(document, document["html"], target)
        if not os.path.exists(target) or os.path.getsize(target) == 0:
            raise OSError("the PDF file was not written")
    except Exception:  # noqa: BLE001
        log.exception("E-PRINT-ESTIMATE: saving estimate %s as PDF failed", estimate_id)
        return PrintResult(False, tr("The PDF could not be saved."))
    return PrintResult(True, tr("Estimate saved as a PDF: {path}", path=target), target)


def print_credit_note(conn, session, return_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print the credit note / exchange slip of a saved return."""
    return _not_connected()


def print_receipt(conn, session, receipt_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print the receipt of a payment received from a customer."""
    return _not_connected()


# ---------------------------------------------------------------- barcode labels (core/labels.py decides what they say)

_LABEL_MARGIN_MM = 1.2
_QUIET_MODULES = 10          # blank space a scanner needs on each side of the bars
_LABEL_FONTS = ["Segoe UI", "Nirmala UI", "Arial", "Tahoma", "DejaVu Sans"]   # Nirmala UI covers Hindi / Marathi
_MAX_MODULE_MM = 0.35        # never draw the bars wider than this (a short code would look odd)


def draw_label(painter, label, width_mm, height_mm, dpmm) -> None:
    """Draw ONE label (shop, product, size and colour, price, MRP, barcode) on a painter.

    dpmm = device pixels per millimetre. The painter's origin is the top-left corner of the label.
    Only draws; it never prints. Text that is too long is shortened with ... so nothing runs off the label.
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainterPath

    from core.money import format_rupees

    def mm(value):
        return value * dpmm

    def text_line(y_mm, h_mm, text, size_mm, *, bold=False, align=Qt.AlignHCenter, x_mm=None, w_mm=None):
        font = QFont()
        font.setFamilies(_LABEL_FONTS)   # named fonts that every Windows PC has; the first one found is used
        font.setPixelSize(max(1, round(mm(size_mm))))
        font.setBold(bold)
        painter.setFont(font)
        x = _LABEL_MARGIN_MM if x_mm is None else x_mm
        w = width_mm - 2 * _LABEL_MARGIN_MM if w_mm is None else w_mm
        metrics = QFontMetricsF(font)
        shown = metrics.elidedText(str(text), Qt.ElideRight, mm(w))
        text_w = metrics.horizontalAdvance(shown)
        if align == Qt.AlignLeft:
            left = mm(x)
        elif align == Qt.AlignRight:
            left = mm(x + w) - text_w
        else:
            left = mm(x) + (mm(w) - text_w) / 2
        baseline = mm(y_mm) + (mm(h_mm) - metrics.height()) / 2 + metrics.ascent()
        # The letters are drawn as filled shapes, not as printer text: a printer driver that has no matching
        # device font (seen with Microsoft Print to PDF) drew solid blocks instead of letters.
        path = QPainterPath()
        path.addText(left, baseline, font, shown)
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("black"))
        painter.drawPath(path)
        painter.restore()

    painter.setPen(QColor("black"))
    inner_w = width_mm - 2 * _LABEL_MARGIN_MM
    y = _LABEL_MARGIN_MM
    if label.get("shop"):
        text_line(y, 2.6, label["shop"], 2.2)
        y += 2.6
    text_line(y, 3.2, label["product"], 2.8, bold=True)
    y += 3.2
    text_line(y, 2.8, f"{label['size']}  \u00b7  {label['colour']}", 2.4)
    y += 2.8
    price_text = format_rupees(label["price_paise"])
    if label.get("mrp_paise") is not None:
        half = inner_w / 2
        text_line(y, 4.2, price_text, 3.8, bold=True, align=Qt.AlignLeft, w_mm=half)
        text_line(y, 4.2, "MRP " + format_rupees(label["mrp_paise"]), 2.2, align=Qt.AlignRight,
                  x_mm=_LABEL_MARGIN_MM + half, w_mm=half)
    else:
        text_line(y, 4.2, price_text, 3.8, bold=True)
    y += 4.2

    # the bars: as wide as the label allows (up to _MAX_MODULE_MM per module), centred, with quiet zones
    bars = label["bars"]
    modules = sum(bars)
    module_mm = min(_MAX_MODULE_MM, inner_w / (modules + 2 * _QUIET_MODULES))
    bars_w = module_mm * modules
    x = (width_mm - bars_w) / 2
    bar_h = max(3.0, height_mm - y - _LABEL_MARGIN_MM - 2.4)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("black"))
    for index, width in enumerate(bars):
        if index % 2 == 0:  # even positions are bars, odd ones are the spaces between them
            painter.drawRect(QRectF(mm(x), mm(y), mm(module_mm * width), mm(bar_h)))
        x += module_mm * width
    painter.setPen(QColor("black"))
    text_line(y + bar_h, 2.4, label["barcode"], 2.0)


def _label_printer(job, *, pdf_path=None):
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize

    size = QPageSize(QSizeF(job["width_mm"], job["height_mm"]), QPageSize.Millimeter)
    layout = QPageLayout(size, QPageLayout.Portrait, QMarginsF(0, 0, 0, 0), QPageLayout.Millimeter)
    if pdf_path:
        # A PDF file needs no printer. QPdfWriter writes it directly and never touches the Windows print
        # system (a QPrinter there crashed with COM error 0x80040155 on a PC with no usable print driver).
        from PySide6.QtGui import QPdfWriter

        writer = QPdfWriter(pdf_path)
        writer.setResolution(600)
        writer.setPageLayout(layout)
        return writer
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

    info = QPrinterInfo.defaultPrinter()
    if info.isNull():
        raise RuntimeError("no printer")
    printer = QPrinter(info, QPrinter.HighResolution)
    printer.setPageLayout(layout)
    return printer


def _paint_labels(printer, job) -> None:
    """One label per page; a label with 3 copies is 3 pages. `printer` is a QPrinter or a QPdfWriter."""
    from PySide6.QtGui import QPainter

    dpmm = printer.logicalDpiX() / 25.4
    painter = QPainter(printer)
    try:
        first = True
        for label in job["labels"]:
            for _ in range(label["copies"]):
                if not first:
                    printer.newPage()
                first = False
                draw_label(painter, label, job["width_mm"], job["height_mm"], dpmm)
    finally:
        painter.end()


def _print_labels(job) -> str:
    """Send the labels to the default printer. Returns the printer's name. Raises RuntimeError if none."""
    printer = _label_printer(job)
    _paint_labels(printer, job)
    return printer.printerName()


def _write_labels_pdf(job, path) -> None:
    _paint_labels(_label_printer(job, pdf_path=path), job)


def _prepare_labels(conn, session, items):
    """The label job, or a PrintResult that says why there is none."""
    from core.labels import build_labels
    from core.permissions import PermissionDenied

    try:
        session.touch()
        return build_labels(conn, session.user_id, items), None
    except PermissionDenied:
        return None, PrintResult(False, tr("You are not allowed to print barcode labels."))
    except ValueError as exc:
        return None, PrintResult(False, tr(str(exc)))
    except Exception:  # noqa: BLE001 - e.g. a locked session
        log.exception("E-PRINT-LABEL: could not prepare labels")
        return None, PrintResult(False, tr("The labels could not be prepared."))


def print_labels(conn, session, items, *, parent=None) -> PrintResult:
    """Print barcode labels. items: [{'variant_id': int, 'copies': int}, ...].

    The label size comes from Settings. Needs the print_labels permission. A printer problem is reported
    here and never undoes anything (the purchase that asked for labels is already saved).
    """
    from core.labels import record_labels_printed

    job, failed = _prepare_labels(conn, session, items)
    if failed:
        return failed
    try:
        printer_name = _print_labels(job)
    except RuntimeError:
        return PrintResult(False, tr("No printer was found on this computer. Connect a label printer and try again."))
    except Exception:  # noqa: BLE001 - a printer problem must never break the screen
        log.exception("E-PRINT-LABEL: printing %s labels failed", job["total"])
        return PrintResult(False, tr("The printer reported a problem."))
    try:
        record_labels_printed(conn, session.user_id, job)
    except Exception:  # noqa: BLE001 - the labels are already out; only the audit note failed
        log.exception("E-PRINT-LABEL: could not write the audit entry")
    return PrintResult(True, tr("{n} label(s) sent to the printer ({printer}).", n=job["total"], printer=printer_name))


def save_labels_pdf(conn, session, items, path) -> PrintResult:
    """Save the labels as a PDF (one label per page) instead of printing, to check them first."""
    from core.labels import record_labels_printed

    job, failed = _prepare_labels(conn, session, items)
    if failed:
        return failed
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _write_labels_pdf(job, path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise OSError("the PDF file was not written")
    except Exception:  # noqa: BLE001
        log.exception("E-PRINT-LABEL: saving the labels as PDF failed")
        return PrintResult(False, tr("The PDF could not be saved."))
    try:
        record_labels_printed(conn, session.user_id, job)
    except Exception:  # noqa: BLE001
        log.exception("E-PRINT-LABEL: could not write the audit entry")
    return PrintResult(True, tr("{n} label(s) saved as a PDF: {path}", n=job["total"], path=path), path)