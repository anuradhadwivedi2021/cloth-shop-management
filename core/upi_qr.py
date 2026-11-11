"""BIL-13: a UPI QR code for the bill or the counter screen, from the shop's own UPI id.

STATIC QR only: the code carries the shop's UPI id, the shop name and (optionally) the amount. The app never
talks to a bank or any network, and it never learns whether the customer paid: the CASHIER checks the money
arrived (in the UPI app or the bank message) and then records the UPI payment on the bill as usual.

  upi_link(upi_id, shop_name, amount_paise=None, note=None)   the text inside the QR (upi://pay?...)
  qr_matrix(text)                                            the dark/light squares of the QR (list of rows)
  qr_png(text, scale=6, border=4)                            the QR as PNG bytes (no Pillow or Qt needed)
  qr_for_bill(profile, amount_paise)                         what a bill or screen needs, or None when the shop
                                                             has no valid UPI id

The QR is made with reportlab's encoder, which the app already uses for barcodes, so nothing new is installed.
"""

import re
import struct
import zlib
from urllib.parse import quote

from reportlab.graphics.barcode import qrencoder

_UPI = re.compile(r"^[A-Za-z0-9._\-]{2,}@[A-Za-z][A-Za-z0-9]{1,}$")
MAX_AMOUNT_PAISE = 10_00_00_000 * 100             # 10 crore rupees: far above any bill, and UPI apps refuse more
MAX_TEXT = 1000


def is_valid_upi_id(value) -> bool:
    return bool(value) and bool(_UPI.match(str(value).strip()))


def upi_link(upi_id, shop_name, amount_paise=None, note=None) -> str:
    """The text a UPI app reads from the QR. Amount is in rupees with 2 decimals (UPI rule)."""
    upi_id = (upi_id or "").strip()
    if not is_valid_upi_id(upi_id):
        raise ValueError("The shop's UPI id is missing or not valid. Set it in Settings.")
    name = " ".join(str(shop_name or "").split())[:40]
    parts = [f"pa={quote(upi_id, safe='@')}"]
    if name:
        parts.append(f"pn={quote(name, safe='')}")
    if amount_paise is not None:
        if isinstance(amount_paise, bool) or not isinstance(amount_paise, int) or amount_paise <= 0:
            raise ValueError("The amount for the QR must be more than zero.")
        if amount_paise > MAX_AMOUNT_PAISE:
            raise ValueError("The amount is too large for a UPI QR.")
        parts.append(f"am={amount_paise // 100}.{amount_paise % 100:02d}")
    parts.append("cu=INR")
    if note:
        parts.append(f"tn={quote(' '.join(str(note).split())[:50], safe='')}")
    return "upi://pay?" + "&".join(parts)


def qr_matrix(text) -> list:
    """The QR as rows of booleans (True = dark square), without the quiet border."""
    text = str(text)
    if not text or len(text) > MAX_TEXT:
        raise ValueError("The QR text is empty or too long.")
    code = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    code.addData(text)
    code.make()
    size = code.getModuleCount()
    return [[bool(code.isDark(r, c)) for c in range(size)] for r in range(size)]


def qr_png(text, scale=6, border=4) -> bytes:
    """The QR as a black-and-white PNG. `border` is the quiet zone in squares (scanners need at least 4)."""
    if not (1 <= scale <= 40) or not (0 <= border <= 20):
        raise ValueError("Bad QR picture size.")
    matrix = qr_matrix(text)
    size = len(matrix) + 2 * border
    rows = []
    for r in range(size):
        if border <= r < size - border:
            cells = [1] * border + [0 if dark else 1 for dark in matrix[r - border]] + [1] * border
        else:
            cells = [1] * size
        bits = [v for cell in cells for v in [cell] * scale]        # 1 = white, 0 = black
        bits += [1] * (-len(bits) % 8)                              # pad the row to whole bytes with white
        packed = bytearray()
        for start in range(0, len(bits), 8):
            value = 0
            for bit in bits[start:start + 8]:
                value = (value << 1) | bit
            packed.append(value)
        rows.extend([b"\x00" + bytes(packed)] * scale)
    width = size * scale

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, width, 1, 0, 0, 0, 0)       # 1 bit, greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


def qr_for_bill(profile, amount_paise=None, *, note=None):
    """{'upi_id', 'link', 'png', 'amount_paise'} for a bill or screen, or None when the shop has no valid UPI id."""
    upi_id = (profile or {}).get("upi_id")
    if not is_valid_upi_id(upi_id):
        return None
    link = upi_link(upi_id, profile.get("name"), amount_paise, note)
    return {"upi_id": upi_id.strip(), "link": link, "png": qr_png(link), "amount_paise": amount_paise}
