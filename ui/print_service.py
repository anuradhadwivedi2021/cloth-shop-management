"""Printing: the one place screens call to print. The real printing (Qt text engine, A4/A5/thermal,
Hindi/Marathi) is built behind these functions; until then they answer "not connected yet".

Rules for callers:
  * Printing happens AFTER the document is saved. A failed print never undoes or repeats a sale:
    show result.message and let the user press Reprint.
  * Reprints are marked "Duplicate copy" by the real implementation. They never create a sale.
  * Always check result.ok; never tell the user "printed" when it is False.
"""

from dataclasses import dataclass

from ui.i18n import tr


@dataclass(frozen=True)
class PrintResult:
    ok: bool
    message: str
    path: str | None = None  # a saved PDF/image, when the result is a file


def _not_connected() -> PrintResult:
    return PrintResult(False, tr("Printing is not connected yet. The document is saved; it can be printed later."))


def print_bill(conn, session, sale_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print a saved bill (tax invoice, bill of supply or simple bill, by its own type)."""
    return _not_connected()


def save_bill_pdf(conn, session, sale_id, path) -> PrintResult:
    """Save a saved bill as a PDF the owner can share by hand."""
    return _not_connected()


def print_credit_note(conn, session, return_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print the credit note / exchange slip of a saved return."""
    return _not_connected()


def print_receipt(conn, session, receipt_id, *, parent=None, duplicate=False) -> PrintResult:
    """Print the receipt of a payment received from a customer."""
    return _not_connected()


def print_labels(conn, session, items, *, parent=None) -> PrintResult:
    """Print barcode labels. items: [{'variant_id': int, 'copies': int}, ...]."""
    return _not_connected()
