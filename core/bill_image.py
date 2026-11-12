"""BIL-11: save a saved bill as a picture (PNG) that the owner can share by hand (WhatsApp, e-mail).

The words, figures and rules of the bill are NOT decided here: they come from core.bill_document.build_bill
(shop details of the bill's date, language on bills, round-off line, tax summary, return policy, ...), so the
picture says exactly what the printed bill and the PDF say. This file only draws that bill with Pillow.
Nothing is sent anywhere; the app writes the one file the user chose.

  render_bill_image(bill)                               the picture of a bill built by build_bill (no database)
  save_bill_image(conn, user_id, sale_id, path, ...)    build, draw, write the file; returns the path

Saving a picture never creates a sale and never touches stock or payments. A copy made after the first one is
saved with duplicate=True: it is marked "Duplicate copy (copy N)" and logged as a reprint, exactly like a
printed duplicate. The first picture changes nothing. The log entry is written only after the file exists, and
if it cannot be written the file is removed again, so you never get one without the other.

Devanagari (Hindi/Marathi) needs a font that has it: Nirmala UI on Windows is used first. If Pillow was built
without the raqm text engine, joined letters can look slightly off: complex_text_supported() tells.
"""

import io
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

from core.bill_document import build_bill
from core.money import format_rupees
from core.permissions import PermissionDenied, has_permission

BILL_VIEW_PERMISSIONS = ("sell", "process_return", "view_reports")
PAPER_WIDTH_PX = {"thermal58": 384, "thermal80": 576, "a5": 874, "a4": 1240}  # about 203 dpi for rolls, 150 dpi for sheets
WIDE_FROM_PX = 800  # sheets get HSN and GST columns; rolls put HSN under the item name

_LATIN_REGULAR = (
    r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\Nirmala.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)
_LATIN_BOLD = (
    r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\NirmalaB.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
)
_DEVANAGARI_REGULAR = (
    r"C:\Windows\Fonts\Nirmala.ttc", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
) + _LATIN_REGULAR
_DEVANAGARI_BOLD = (
    r"C:\Windows\Fonts\NirmalaB.ttc", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
) + _LATIN_BOLD


def complex_text_supported() -> bool:
    """True when Pillow can join Devanagari letters correctly (raqm engine present)."""
    return bool(features.check("raqm"))


def _font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


class _Sheet:
    """Collects what to draw, then draws it on a picture exactly as tall as it needs to be."""

    def __init__(self, width, devanagari, base):
        self.width, self.pad = width, max(12, width // 36)
        regular, bold = (_DEVANAGARI_REGULAR, _DEVANAGARI_BOLD) if devanagari else (_LATIN_REGULAR, _LATIN_BOLD)
        self.fonts = {
            "r": _font(regular, base), "b": _font(bold, base), "big": _font(bold, int(base * 1.35)),
            "small": _font(regular, int(base * 0.8)),
        }
        self.ops, self.y = [], self.pad
        self._m = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def size(self, text, font):
        box = self._m.textbbox((0, 0), text, font=self.fonts[font])
        return box[2] - box[0], max(box[3], self.fonts[font].size)

    def wrap(self, text, font, max_width):
        lines, current = [], ""
        for word in str(text).split():
            trial = f"{current} {word}".strip()
            if current and self.size(trial, font)[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return lines or [""]

    def put(self, text, *, font="r", align="left", x=None, right=None, y=None):
        w, h = self.size(text, font)
        y = self.y if y is None else y
        if align == "center":
            x = (self.width - w) / 2
        elif align == "right":
            x = (self.width - self.pad if right is None else right) - w
        elif x is None:
            x = self.pad
        self.ops.append(("text", x, y, text, font))
        return h

    def paragraph(self, text, *, font="r", align="left", gap=4):
        for part in self.wrap(text, font, self.width - 2 * self.pad):
            self.y += self.put(part, font=font, align=align) + gap

    def pair(self, left, right, *, font="r", gap=4):
        h = self.put(left, font=font)
        self.put(right, font=font, align="right")
        self.y += h + gap

    def rule(self, gap=8):
        self.y += gap // 2
        self.ops.append(("rule", self.y))
        self.y += gap // 2 + 2

    def banner(self, text):
        w, h = self.size(text, "b")
        top = self.y
        self.ops.append(("box", (self.width - w) / 2 - 10, top, (self.width + w) / 2 + 10, top + h + 12))
        self.ops.append(("text", (self.width - w) / 2, top + 4, text, "b"))
        self.y = top + h + 22

    def picture(self, png, side):
        """Centre a small PNG (the UPI QR) at `side` pixels square."""
        self.ops.append(("picture", (self.width - side) / 2, self.y, png, side))
        self.y += side + 6

    def draw(self):
        image = Image.new("RGB", (self.width, int(self.y + self.pad)), "white")
        canvas = ImageDraw.Draw(image)
        for op in self.ops:
            if op[0] == "text":
                canvas.text((op[1], op[2]), op[3], font=self.fonts[op[4]], fill="black")
            elif op[0] == "rule":
                canvas.line([(self.pad, op[1]), (self.width - self.pad, op[1])], fill="black", width=1)
            elif op[0] == "box":
                canvas.rectangle(op[1:], outline="black", width=2)
            elif op[0] == "picture":
                small = Image.open(io.BytesIO(op[3])).convert("RGB").resize((op[4], op[4]), Image.NEAREST)
                image.paste(small, (int(op[1]), int(op[2])))
        return image


def _has_devanagari(*texts) -> bool:
    return any("\u0900" <= ch <= "\u097f" for text in texts for ch in str(text or ""))


def _rate_text(rate) -> str:
    return f"{rate:g}%"


def render_bill_image(bill, *, width_px=None) -> Image.Image:
    """Draw a bill made by core.bill_document.build_bill. Width follows the paper in settings unless given."""
    width = width_px or PAPER_WIDTH_PX.get(bill["paper"]["name"], PAPER_WIDTH_PX["thermal80"])
    if not isinstance(width, int) or isinstance(width, bool) or not 300 <= width <= 2000:
        raise ValueError("Picture width must be between 300 and 2000 pixels.")
    w, shop = bill["words"], bill["shop"]
    show_tax, wide = bill["show_tax"], width >= WIDE_FROM_PX
    customer = bill["customer"]
    devanagari = bill["language"] in ("hi", "mr") or _has_devanagari(
        shop.get("name"), shop.get("address"), customer and customer.get("name"),
        *(line["name"] for line in bill["lines"]), bill["footer"],
    )
    spec = bill["paper"]
    dpi = 203 if spec["roll"] else 150  # thermal heads print at about 203 dpi; sheets are drawn at 150 dpi
    sheet = _Sheet(width, devanagari, max(14, min(round(spec["font_pt"] * dpi / 72), width // 16)))

    if shop.get("name"):
        sheet.paragraph(shop["name"], font="big", align="center", gap=6)
    for text in (shop.get("address"), shop.get("phone") and f"{w['phone']}: {shop['phone']}",
                 show_tax and shop.get("gstin") and f"{w['gstin']}: {shop['gstin']}"):
        if text:
            sheet.paragraph(text, font="small", align="center", gap=2)
    sheet.rule(14)
    sheet.paragraph(bill["title"], font="b", align="center", gap=6)
    if bill["is_duplicate"]:
        sheet.banner(bill["copy_label"])
    sheet.pair(f"{w['invoice_no']} {bill['invoice_no']}", bill["when"], font="small")
    if customer and customer.get("name"):
        sheet.paragraph(f"{w['customer']}: {customer['name']}", font="small", gap=2)
        if show_tax and customer.get("gstin"):
            sheet.paragraph(f"{w['gstin']}: {customer['gstin']}", font="small", gap=2)
    sheet.rule()

    amount_x = width - sheet.pad
    if wide and show_tax:
        columns = [("hsn", width * 0.42), ("qty", width * 0.50), ("rate", width * 0.62), ("gst", width * 0.70),
                   ("taxable", width * 0.84), ("amount", amount_x)]
        item_room = width * 0.42 - 2 * sheet.pad - sheet.size("00", "r")[0]
    elif wide:
        columns = [("qty", width * 0.62), ("rate", width * 0.78), ("amount", amount_x)]
        item_room = width * 0.62 - 2 * sheet.pad - sheet.size("00", "r")[0]
    else:
        columns = [("qty", width * 0.56), ("rate", width * 0.77), ("amount", amount_x)]
        item_room = width * 0.56 - 2 * sheet.pad - sheet.size("00", "r")[0]

    top = sheet.y
    sheet.put(w["item"], font="b", y=top)
    for key, x in columns:
        sheet.put(w[key], font="b", align="right", right=x, y=top)
    sheet.y += sheet.size(w["item"], "b")[1] + 6
    sheet.rule(6)
    for line in bill["lines"]:
        parts = sheet.wrap(line["name"], "r", item_room)
        top = sheet.y
        for part in parts:
            sheet.y += sheet.put(part, y=sheet.y) + 2
        cells = {"hsn": line["hsn"], "qty": line["qty_text"], "rate": format_rupees(line["rate_shown_paise"], symbol=""),
                 "gst": _rate_text(line["rate_percent"]), "taxable": format_rupees(line["taxable_paise"], symbol=""),
                 "amount": format_rupees(line["line_total_paise"], symbol="")}
        for key, x in columns:
            sheet.put(cells[key], align="right", right=x, y=top)
        notes = []
        if show_tax and not wide and line["hsn"]:
            notes.append(f"{w['hsn']} {line['hsn']}")
        if line["discount_paise"]:
            notes.append(f"{w['discount']} -{format_rupees(line['discount_paise'], symbol='')}")
        if notes:
            sheet.paragraph("  ".join(notes), font="small", gap=2)
        sheet.y += 4
    sheet.rule()

    def row(label, paise, font="r"):
        sheet.pair(label, format_rupees(paise, symbol=""), font=font)

    row(w["items_total"], bill["subtotal_paise"])
    if bill["discount_paise"]:
        row(w["discount_total"], -bill["discount_paise"])
    if show_tax:
        row(w["taxable_value"], bill["taxable_paise"])
        if bill["intra_state"]:
            row(w["cgst"], bill["cgst_paise"])
            row(w["sgst"], bill["sgst_paise"])
        else:
            row(w["igst"], bill["igst_paise"])
    if bill["round_off_paise"]:
        row(w["round_off"], bill["round_off_paise"])
    sheet.rule(6)
    sheet.pair(w["total"], format_rupees(bill["total_paise"]), font="big", gap=8)
    sheet.rule()
    for pay in bill["payments"]:
        row(pay["label"], pay["amount_paise"], font="small")
    if bill["change_paise"]:
        row(w["change"], bill["change_paise"], font="small")
    if bill["due_paise"] > 0:
        row(w["balance_due"], bill["due_paise"], font="b")
    sheet.y += 6
    if bill.get("upi"):                       # BIL-13: static UPI QR for the amount still to pay
        sheet.paragraph(w["scan_to_pay"], font="b", align="center", gap=4)
        sheet.picture(bill["upi"]["png"], max(180, min(width // 2, 320)))
        sheet.paragraph(f"{w['upi_to']}: {bill['upi']['upi_id']}", font="small", align="center", gap=6)
    notes = []
    if bill["tax_mode"] == "composition":
        notes.append(w["composition"])
    if show_tax and bill["prices_include_tax"]:
        notes.append(w["gst_included"])
    notes += [t for t in (bill["policy"], bill["footer"]) if t]
    for text in notes:
        sheet.paragraph(text, font="small", align="center", gap=3)
    sheet.paragraph(w["thanks"], font="small", align="center")
    return sheet.draw()


def _require_access(conn, user_id):
    if not any(has_permission(conn, user_id, p) for p in BILL_VIEW_PERMISSIONS):
        raise PermissionDenied("You are not allowed to save bills.")


def save_bill_image(conn, user_id, sale_id, path, *, duplicate=False, width_px=None) -> str:
    """Save a saved bill as a PNG at `path` and return the path (only after the file really exists).

    duplicate=True marks the picture as a copy and logs the reprint (like a printed duplicate); the first
    picture (duplicate=False) logs nothing. A bad name, a missing folder or a file that cannot be written
    raises ValueError with a short message and leaves no file and no log entry behind.
    """
    _require_access(conn, user_id)
    target = Path(path)
    if target.suffix.lower() != ".png":
        raise ValueError("Save the bill picture with a .png file name.")
    if not target.parent.is_dir():
        raise ValueError("That folder does not exist.")
    bill = build_bill(conn, sale_id)  # read only; raises ValueError("Bill not found.")
    if duplicate:
        copies = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'sale_reprint' AND entity = 'sale' AND entity_id = ?",
            (sale_id,),
        ).fetchone()[0] + 1
        bill["is_duplicate"] = True
        bill["copy_label"] = f"{bill['words']['duplicate_copy']} ({bill['words']['copy_no'].format(n=copies)})"
    image = render_bill_image(bill, width_px=width_px)

    temporary = target.with_name(target.name + ".part")
    try:
        image.save(temporary, format="PNG")
        os.replace(temporary, target)
    except OSError:
        _remove(temporary)
        raise ValueError("The picture could not be saved. Check the folder and try again.") from None
    if duplicate:
        try:
            build_bill(conn, sale_id, user_id=user_id, duplicate=True)  # logs the reprint (its own transaction)
        except Exception:
            _remove(target)
            raise ValueError("The picture could not be saved. Nothing was changed.") from None
    return str(target)


def _remove(path):
    try:
        Path(path).unlink()
    except OSError:
        pass