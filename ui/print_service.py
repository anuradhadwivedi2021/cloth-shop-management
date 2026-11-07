"""Printing: the one place screens call to print.

Connected now:      print_bill (also reprints), save_bill_pdf
Still "not connected yet" (own steps): print_credit_note, print_receipt, print_labels

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
    from PySide6.QtPrintSupport import QPrinter

    doc, size = _page_for(bill, html)
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(path)
    _setup_printer(printer, bill, size)
    doc.print_(printer)


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


def print_credit_note(conn, session, return_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print the credit note / exchange slip of a saved return."""
    return _not_connected()


def print_receipt(conn, session, receipt_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print the receipt of a payment received from a customer."""
    return _not_connected()


def print_labels(conn, session, items, *, parent=None) -> PrintResult:
    """Print barcode labels. items: [{'variant_id': int, 'copies': int}, ...]."""
    return _not_connected()