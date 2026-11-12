"""What a printed bill says: the saved sale turned into a ready-to-print document.

This file has no screens and no printer code, so every figure and word on the bill is tested.
`ui/print_service.py` only hands the result to the printer or saves it as a PDF.

  build_bill(conn, sale_id, user_id=..., duplicate=True)   the saved bill, shop details of ITS date
  bill_html(bill)                                           the bill as HTML for the paper in settings
  bill_file_name(bill)                                      e.g. 'INV-2026-27-0001.pdf'

Rules (SRS BIL-06, BIL-09, BIL-11, BIL-15):
  * Nothing here changes a sale, a number, stock or a payment. A duplicate copy is only logged.
  * The shop name, address and GSTIN are the ones valid on the bill's date (old bills keep their details).
  * Tax invoice (regular GST) shows HSN, GST rate, taxable value and CGST + SGST or IGST.
    Bill of supply and simple bill show no tax columns.
  * Round-off is its own line. All money is whole paise and is formatted in Indian grouping.
  * The words on the bill follow the "Language on bills" setting (English, Hindi or Marathi),
    which can differ from the language of the screens.
"""

import base64
from html import escape

from core import fabric
from core.money import format_rupees
from core.sales import get_sale, reprint_sale
from core.settings import get_bool, get_setting
from core.shop_settings import get_shop_profile
from core.upi_qr import qr_for_bill

LANGUAGES = ("en", "hi", "mr")

# paper -> width/height in mm (height None = roll paper, as long as the bill), margin in mm, font size in pt
PAPERS = {
    "a4": {"width_mm": 210, "height_mm": 297, "margin_mm": 12, "font_pt": 10, "roll": False},
    "a5": {"width_mm": 148, "height_mm": 210, "margin_mm": 8, "font_pt": 9, "roll": False},
    "thermal80": {"width_mm": 80, "height_mm": None, "margin_mm": 3, "font_pt": 9, "roll": True},
    "thermal58": {"width_mm": 58, "height_mm": None, "margin_mm": 2, "font_pt": 8, "roll": True},
}
DEFAULT_PAPER = "thermal80"

_TITLE_KEY = {"regular": "tax_invoice", "composition": "bill_of_supply", "unregistered": "bill"}

_TEXT = {
    "en": {
        "tax_invoice": "TAX INVOICE", "bill_of_supply": "BILL OF SUPPLY", "bill": "BILL",
        "invoice_no": "Bill no.", "date": "Date", "customer": "Customer", "phone": "Phone", "gstin": "GSTIN",
        "item": "Item", "hsn": "HSN", "qty": "Qty", "rate": "Rate", "discount": "Disc.", "taxable": "Taxable",
        "gst": "GST", "amount": "Amount",
        "items_total": "Items total", "discount_total": "Discount", "taxable_value": "Taxable value",
        "cgst": "CGST", "sgst": "SGST", "igst": "IGST", "round_off": "Round off", "total": "TOTAL",
        "paid": "Paid", "change": "Change returned", "balance_due": "BALANCE DUE",
        "thanks": "Thank you! Please visit again.",
        "duplicate_copy": "Duplicate copy", "copy_no": "copy {n}",
        "composition": "Composition taxable person, not eligible to collect tax on supplies.",
        "gst_included": "Prices include GST", "scan_to_pay": "Scan to pay by UPI", "upi_to": "UPI",
        "policy_days": "Returns accepted within {days} days with this bill.",
        "policy_open": "Returns accepted with this bill.",
        "policy_final": "Not returnable: {categories}.",
        "mode_cash": "Cash", "mode_upi": "UPI", "mode_card": "Card", "mode_bank_transfer": "Bank transfer",
        "mode_cheque": "Cheque", "mode_other": "Other", "mode_store_credit": "Store credit",
    },
    "hi": {
        "tax_invoice": "टैक्स इनवॉइस", "bill_of_supply": "आपूर्ति बिल", "bill": "बिल",
        "invoice_no": "बिल नं.", "date": "दिनांक", "customer": "ग्राहक", "phone": "फोन", "gstin": "GSTIN",
        "item": "सामान", "hsn": "HSN", "qty": "मात्रा", "rate": "दर", "discount": "छूट", "taxable": "कर योग्य",
        "gst": "GST", "amount": "राशि",
        "items_total": "सामान का जोड़", "discount_total": "छूट", "taxable_value": "कर योग्य मूल्य",
        "cgst": "CGST", "sgst": "SGST", "igst": "IGST", "round_off": "राउंड ऑफ", "total": "कुल",
        "paid": "भुगतान", "change": "वापस किया", "balance_due": "बकाया राशि",
        "thanks": "धन्यवाद! फिर पधारें।",
        "duplicate_copy": "डुप्लिकेट कॉपी", "copy_no": "कॉपी {n}",
        "composition": "कंपोज़िशन कर-दाता; आपूर्ति पर कर वसूलने का अधिकार नहीं।",
        "gst_included": "कीमत में GST शामिल है", "scan_to_pay": "UPI से भुगतान के लिए स्कैन करें", "upi_to": "UPI",
        "policy_days": "इस बिल के साथ {days} दिन के भीतर वापसी स्वीकार है।",
        "policy_open": "इस बिल के साथ वापसी स्वीकार है।",
        "policy_final": "वापस नहीं होगा: {categories}।",
        "mode_cash": "नकद", "mode_upi": "UPI", "mode_card": "कार्ड", "mode_bank_transfer": "बैंक ट्रांसफर",
        "mode_cheque": "चेक", "mode_other": "अन्य", "mode_store_credit": "स्टोर क्रेडिट",
    },
    "mr": {
        "tax_invoice": "कर बीजक", "bill_of_supply": "पुरवठा बिल", "bill": "बिल",
        "invoice_no": "बिल क्र.", "date": "दिनांक", "customer": "ग्राहक", "phone": "फोन", "gstin": "GSTIN",
        "item": "वस्तू", "hsn": "HSN", "qty": "संख्या", "rate": "दर", "discount": "सूट", "taxable": "करपात्र",
        "gst": "GST", "amount": "रक्कम",
        "items_total": "वस्तूंची बेरीज", "discount_total": "सूट", "taxable_value": "करपात्र मूल्य",
        "cgst": "CGST", "sgst": "SGST", "igst": "IGST", "round_off": "पूर्णांक", "total": "एकूण",
        "paid": "भरलेले", "change": "परत दिले", "balance_due": "बाकी रक्कम",
        "thanks": "धन्यवाद! पुन्हा भेट द्या.",
        "duplicate_copy": "डुप्लिकेट प्रत", "copy_no": "प्रत {n}",
        "composition": "कंपोझिशन करदाता; पुरवठ्यावर कर आकारण्यास पात्र नाही.",
        "gst_included": "किमतीत GST समाविष्ट आहे", "scan_to_pay": "UPI ने पैसे देण्यासाठी स्कॅन करा", "upi_to": "UPI",
        "policy_days": "या बिलासह {days} दिवसांत परतावा स्वीकारला जाईल.",
        "policy_open": "या बिलासह परतावा स्वीकारला जाईल.",
        "policy_final": "परत घेतले जाणार नाही: {categories}.",
        "mode_cash": "रोख", "mode_upi": "UPI", "mode_card": "कार्ड", "mode_bank_transfer": "बँक हस्तांतरण",
        "mode_cheque": "धनादेश", "mode_other": "इतर", "mode_store_credit": "स्टोअर क्रेडिट",
    },
}


def paper_spec(paper) -> dict:
    """Size and font for a paper name from settings; an unknown name falls back to 80 mm thermal."""
    return dict(PAPERS.get(paper) or PAPERS[DEFAULT_PAPER], name=paper if paper in PAPERS else DEFAULT_PAPER)


def _words(lang) -> dict:
    return _TEXT.get(lang) or _TEXT["en"]


def _when(stamp) -> str:
    """'2026-11-03 18:30:05' -> '03/11/2026 18:30'."""
    try:
        day, clock = str(stamp).split(" ")[:2]
        y, m, d = day.split("-")
        return f"{d}/{m}/{y} {clock[:5]}"
    except (ValueError, AttributeError):
        return str(stamp or "")


def _policy_text(conn, words) -> str:
    """The return-policy sentence(s), from the same settings the return rules use."""
    raw = get_setting(conn, "return_window_days")
    try:
        days = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        days = None
    text = words["policy_days"].format(days=days) if days is not None else words["policy_open"]
    categories = [c.strip() for c in (get_setting(conn, "final_sale_categories") or "").split(",") if c.strip()]
    if categories:
        text += " " + words["policy_final"].format(categories=", ".join(categories))
    return text


def build_bill(conn, sale_id, *, user_id=None, duplicate=False) -> dict:
    """Everything the bill shows, read from the saved sale. Raises ValueError if the bill is not found.

    duplicate=True marks the copy "Duplicate copy" and logs the reprint (needs user_id).
    A normal first print (duplicate=False) changes nothing at all.
    """
    if duplicate:
        if user_id is None:
            raise ValueError("A reprint needs to know who is reprinting.")
        data = reprint_sale(conn, sale_id, user_id=user_id)
        copy_number = data["copy_number"]
    else:
        data = get_sale(conn, sale_id)
        copy_number = None

    sale = data["sale"]
    lang = get_setting(conn, "bill_language") or "en"
    lang = lang if lang in LANGUAGES else "en"
    words = _words(lang)
    paper = get_setting(conn, "bill_paper") or DEFAULT_PAPER
    mode = sale["tax_mode"]
    show_tax = mode == "regular"

    on_date = str(sale["created_at"])[:10]
    shop = get_shop_profile(conn, on_date)

    customer = None
    if sale["customer_id"] is not None:
        row = conn.execute("SELECT name, phone, gstin FROM customers WHERE id = ?", (sale["customer_id"],)).fetchone()
        if row is not None:
            customer = {"name": row["name"], "phone": row["phone"], "gstin": row["gstin"]}

    lines = []
    summary = {}  # GST rate -> taxable / cgst / sgst / igst (tax invoice only)
    for n, line in enumerate(data["lines"], start=1):
        unit_row = conn.execute("SELECT p.unit FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = ?",
                                (line["variant_id"],)).fetchone()
        unit = unit_row["unit"] if unit_row else "piece"
        lines.append({
            "qty_text": fabric.qty_text(unit, line["qty"]),                        # fabric: "2.5 m"
            "rate_shown_paise": fabric.metre_price(unit, line["unit_price_paise"]),  # fabric: per metre
            "no": n,
            "name": " ".join(part for part in (line["product_name"], line["size"], line["colour"]) if part),
            "hsn": line["hsn"] or "",
            "qty": line["qty"],
            "unit_price_paise": line["unit_price_paise"],
            "discount_paise": line["discount_paise"],
            "taxable_paise": line["taxable_paise"],
            "rate_percent": line["tax_rate_percent"],
            "line_total_paise": line["line_total_paise"],
        })
        if show_tax:
            bucket = summary.setdefault(
                line["tax_rate_percent"], {"rate_percent": line["tax_rate_percent"], "taxable_paise": 0,
                                           "cgst_paise": 0, "sgst_paise": 0, "igst_paise": 0})
            bucket["taxable_paise"] += line["taxable_paise"]
            bucket["cgst_paise"] += line["cgst_paise"]
            bucket["sgst_paise"] += line["sgst_paise"]
            bucket["igst_paise"] += line["igst_paise"]

    payments = [
        {"mode": p["mode"], "label": words.get("mode_" + p["mode"], p["mode"]), "amount_paise": p["amount_paise"]}
        for p in data["payments"] if not p.get("reversal_of")
    ]

    upi = None            # BIL-13: a static UPI QR for the amount still to pay (the cashier confirms the money)
    if data["due_paise"] > 0 and get_bool(conn, "show_upi_qr_on_bill", True):
        upi = qr_for_bill(shop, data["due_paise"], note=sale["invoice_no"])

    footer = (get_setting(conn, "bill_footer") or "").strip()
    policy = _policy_text(conn, words) if get_bool(conn, "show_return_policy_on_bill", True) else ""

    return {
        "sale_id": sale["id"],
        "invoice_no": sale["invoice_no"],
        "when": _when(sale["created_at"]),
        "tax_mode": mode,
        "title": words[_TITLE_KEY.get(mode, "bill")],
        "show_tax": show_tax,
        "intra_state": bool(sale["intra_state"]),
        "prices_include_tax": bool(sale["prices_include_tax"]),
        "shop": shop,
        "customer": customer,
        "lines": lines,
        "tax_summary": sorted(summary.values(), key=lambda b: b["rate_percent"]),
        "subtotal_paise": sale["subtotal_paise"],
        "discount_paise": sale["discount_paise"],
        "taxable_paise": sale["taxable_paise"],
        "tax_paise": sale["tax_paise"],
        "cgst_paise": sum(b["cgst_paise"] for b in summary.values()),
        "sgst_paise": sum(b["sgst_paise"] for b in summary.values()),
        "igst_paise": sum(b["igst_paise"] for b in summary.values()),
        "round_off_paise": sale["round_off_paise"],
        "total_paise": sale["total_paise"],
        "payments": payments,
        "paid_paise": data["paid_paise"],
        "change_paise": sale["change_paise"] or 0,
        "due_paise": data["due_paise"],
        "upi": upi,
        "footer": footer,
        "policy": policy,
        "is_duplicate": bool(duplicate),
        "copy_label": words["duplicate_copy"] + " (" + words["copy_no"].format(n=copy_number) + ")" if duplicate else "",
        "language": lang,
        "paper": paper_spec(paper),
        "words": words,
    }


def bill_file_name(bill) -> str:
    """A safe file name for a saved PDF: 'INV-2026-27-0001.pdf' ('/' and other odd characters become '-')."""
    base = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(bill["invoice_no"]))
    return (base or f"bill-{bill['sale_id']}") + ("-duplicate" if bill["is_duplicate"] else "") + ".pdf"


# ---------------------------------------------------------------- HTML

def _e(value) -> str:
    return escape(str(value if value is not None else ""))


def _money(paise) -> str:
    return format_rupees(paise)


def bill_html(bill) -> str:
    """The bill as HTML (tables only, so Qt's text engine and any browser draw it the same way)."""
    w = bill["words"]
    spec = bill["paper"]
    roll = spec["roll"]
    narrow = roll and spec["width_mm"] <= 58
    show_tax = bill["show_tax"]
    shop = bill["shop"]
    size = spec["font_pt"]
    out = [f'<html><head><meta charset="utf-8"></head><body style="font-size:{size}pt;">']

    # ----- header
    out.append(f'<p align="center"><b style="font-size:{size + 3}pt;">{_e(shop.get("name") or "")}</b>')
    for key in ("address", "phone"):
        if shop.get(key):
            out.append(f"<br>{_e(shop[key])}")
    if shop.get("gstin") and bill["tax_mode"] != "unregistered":
        out.append(f'<br>{_e(w["gstin"])}: {_e(shop["gstin"])}')
    out.append("</p>")
    out.append(f'<p align="center"><b>{_e(bill["title"])}</b>')
    if bill["is_duplicate"]:
        out.append(f'<br><b>*** {_e(bill["copy_label"])} ***</b>')
    out.append("</p>")

    out.append('<table width="100%" cellspacing="0" cellpadding="1">')
    if roll:  # a roll is too narrow for the number and the date side by side
        out.append(f'<tr><td>{_e(w["invoice_no"])}: <b>{_e(bill["invoice_no"])}</b></td></tr>')
        out.append(f'<tr><td>{_e(w["date"])}: {_e(bill["when"])}</td></tr>')
    else:
        out.append(f'<tr><td>{_e(w["invoice_no"])}: <b>{_e(bill["invoice_no"])}</b></td>'
                   f'<td align="right">{_e(w["date"])}: {_e(bill["when"])}</td></tr>')
    cust = bill["customer"]
    if cust:
        who = _e(cust["name"] or "")
        if cust["phone"]:
            who += f' &nbsp;{_e(w["phone"])}: {_e(cust["phone"])}'
        out.append(f'<tr><td colspan="2">{_e(w["customer"])}: {who}</td></tr>')
        if cust["gstin"]:
            out.append(f'<tr><td colspan="2">{_e(w["gstin"])}: {_e(cust["gstin"])}</td></tr>')
    out.append("</table><hr>")

    # ----- items
    if roll:
        out.append('<table width="100%" cellspacing="0" cellpadding="1">')
        for line in bill["lines"]:
            out.append(f'<tr><td colspan="2"><b>{line["no"]}. {_e(line["name"])}</b></td></tr>')
            detail = f'{_e(line["qty_text"])} x {_e(_money(line["rate_shown_paise"]))}'
            if show_tax and not narrow:
                detail += f' &nbsp;({_e(w["hsn"])} {_e(line["hsn"])}, {_e(w["gst"])} {line["rate_percent"]:g}%)'
            out.append(f'<tr><td>{detail}</td><td align="right">{_e(_money(line["line_total_paise"]))}</td></tr>')
            if line["discount_paise"]:
                out.append(f'<tr><td>&nbsp;&nbsp;{_e(w["discount"])}</td>'
                           f'<td align="right">-{_e(_money(line["discount_paise"]))}</td></tr>')
        out.append("</table>")
    else:
        head = [w["item"]] + ([w["hsn"]] if show_tax else []) + [w["qty"], w["rate"], w["discount"]]
        if show_tax:
            head += [w["taxable"], w["gst"]]
        head.append(w["amount"])
        out.append('<table width="100%" border="1" cellspacing="0" cellpadding="3"><tr>')
        for i, text in enumerate(head):
            out.append(f'<th align="{"left" if i == 0 else "right"}">{_e(text)}</th>')
        out.append("</tr>")
        for line in bill["lines"]:
            cells = [f'{line["no"]}. {_e(line["name"])}']
            if show_tax:
                cells.append(_e(line["hsn"]))
            cells += [_e(line["qty_text"]), _e(_money(line["rate_shown_paise"])),
                      _e(_money(line["discount_paise"])) if line["discount_paise"] else ""]
            if show_tax:
                cells += [_e(_money(line["taxable_paise"])), f'{line["rate_percent"]:g}%']
            cells.append(_e(_money(line["line_total_paise"])))
            out.append("<tr>" + "".join(
                f'<td align="{"left" if i == 0 else "right"}">{c}</td>' for i, c in enumerate(cells)) + "</tr>")
        out.append("</table>")
    out.append("<hr>")

    # ----- totals
    rows = [(w["items_total"], _money(bill["subtotal_paise"]), False)]
    if bill["discount_paise"]:
        rows.append((w["discount_total"], "-" + _money(bill["discount_paise"]), False))
    if show_tax:
        rows.append((w["taxable_value"], _money(bill["taxable_paise"]), False))
        if bill["intra_state"]:
            rows.append((w["cgst"], _money(bill["cgst_paise"]), False))
            rows.append((w["sgst"], _money(bill["sgst_paise"]), False))
        else:
            rows.append((w["igst"], _money(bill["igst_paise"]), False))
    if bill["round_off_paise"]:
        sign = "-" if bill["round_off_paise"] < 0 else "+"
        rows.append((w["round_off"], sign + _money(abs(bill["round_off_paise"])), False))
    rows.append((w["total"], _money(bill["total_paise"]), True))
    out.append('<table width="100%" cellspacing="0" cellpadding="1">')
    for name, amount, strong in rows:
        if strong:
            out.append(f'<tr><td><b style="font-size:{size + 2}pt;">{_e(name)}</b></td>'
                       f'<td align="right"><b style="font-size:{size + 2}pt;">{_e(amount)}</b></td></tr>')
        else:
            out.append(f'<tr><td>{_e(name)}</td><td align="right">{_e(amount)}</td></tr>')
    out.append("</table>")
    if show_tax and bill["prices_include_tax"]:
        out.append(f'<p align="right"><i>{_e(w["gst_included"])}</i></p>')

    # ----- GST by rate (A4 / A5 tax invoice)
    if show_tax and not roll and bill["tax_summary"]:
        out.append('<br><table width="100%" border="1" cellspacing="0" cellpadding="2"><tr>'
                   f'<th align="left">{_e(w["gst"])} %</th><th align="right">{_e(w["taxable_value"])}</th>')
        if bill["intra_state"]:
            out.append(f'<th align="right">{_e(w["cgst"])}</th><th align="right">{_e(w["sgst"])}</th></tr>')
        else:
            out.append(f'<th align="right">{_e(w["igst"])}</th></tr>')
        for b in bill["tax_summary"]:
            out.append(f'<tr><td>{b["rate_percent"]:g}%</td><td align="right">{_e(_money(b["taxable_paise"]))}</td>')
            if bill["intra_state"]:
                out.append(f'<td align="right">{_e(_money(b["cgst_paise"]))}</td>'
                           f'<td align="right">{_e(_money(b["sgst_paise"]))}</td></tr>')
            else:
                out.append(f'<td align="right">{_e(_money(b["igst_paise"]))}</td></tr>')
        out.append("</table>")

    # ----- payments
    out.append("<hr>")
    out.append('<table width="100%" cellspacing="0" cellpadding="1">')
    for p in bill["payments"]:
        out.append(f'<tr><td>{_e(p["label"])}</td><td align="right">{_e(_money(p["amount_paise"]))}</td></tr>')
    if bill["change_paise"]:
        out.append(f'<tr><td>{_e(w["change"])}</td><td align="right">{_e(_money(bill["change_paise"]))}</td></tr>')
    if bill["due_paise"] > 0:
        out.append(f'<tr><td><b>{_e(w["balance_due"])}</b></td>'
                   f'<td align="right"><b>{_e(_money(bill["due_paise"]))}</b></td></tr>')
    out.append("</table>")

    # ----- UPI QR (only on a bill that still has an amount to pay)
    if bill.get("upi"):
        side = 150 if roll and not narrow else (110 if narrow else 130)
        data_uri = "data:image/png;base64," + base64.b64encode(bill["upi"]["png"]).decode("ascii")
        out.append(f'<p align="center"><b>{_e(w["scan_to_pay"])}</b><br>'
                   f'<img src="{data_uri}" width="{side}" height="{side}"><br>'
                   f'{_e(w["upi_to"])}: {_e(bill["upi"]["upi_id"])}</p>')

    # ----- footer
    if bill["tax_mode"] == "composition":
        out.append(f'<p align="center"><i>{_e(w["composition"])}</i></p>')
    if bill["policy"]:
        out.append(f'<p align="center">{_e(bill["policy"])}</p>')
    if bill["footer"]:
        out.append(f'<p align="center">{_e(bill["footer"])}</p>')
    out.append(f'<p align="center"><b>{_e(w["thanks"])}</b></p>')
    out.append("</body></html>")
    return "".join(out)