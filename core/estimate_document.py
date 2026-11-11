"""What a printed estimate says. No screens, no printer: tested like the bill (core/bill_document.py).

An estimate is a price quote. It is NOT a tax invoice, and it says so on paper. Paper size and the language of
the words follow the same Settings as bills.
"""

from html import escape

from core import estimates as est
from core.bill_document import _when, paper_spec
from core.money import format_rupees
from core.settings import get_setting
from core.shop_settings import get_shop_profile

_WORDS = {
    "en": {"title": "ESTIMATE", "not_invoice": "This is an estimate, not a tax invoice.", "no": "Estimate no.",
           "valid": "Valid until", "customer": "Customer", "item": "Item", "qty": "Qty", "rate": "Rate",
           "amount": "Amount", "total": "ESTIMATE TOTAL", "note": "Note", "phone": "Phone", "expired": "EXPIRED",
           "prices": "Prices are as quoted on the estimate date and may change after the validity date."},
    "hi": {"title": "अनुमान (एस्टिमेट)", "not_invoice": "यह अनुमान है, टैक्स इनवॉइस नहीं।", "no": "अनुमान संख्या",
           "valid": "मान्य तारीख तक", "customer": "ग्राहक", "item": "सामान", "qty": "संख्या", "rate": "दर",
           "amount": "रकम", "total": "अनुमान कुल", "note": "टिप्पणी", "phone": "फ़ोन", "expired": "समाप्त",
           "prices": "कीमतें अनुमान की तारीख की हैं और मान्यता तारीख के बाद बदल सकती हैं।"},
    "mr": {"title": "अंदाज (एस्टिमेट)", "not_invoice": "हा अंदाज आहे, टॅक्स इनव्हॉइस नाही.", "no": "अंदाज क्रमांक",
           "valid": "पर्यंत वैध", "customer": "ग्राहक", "item": "वस्तू", "qty": "संख्या", "rate": "दर",
           "amount": "रक्कम", "total": "अंदाज एकूण", "note": "टीप", "phone": "फोन", "expired": "मुदत संपली",
           "prices": "किमती अंदाजाच्या तारखेच्या आहेत आणि वैधता तारखेनंतर बदलू शकतात."},
}


def _e(text) -> str:
    return escape(str(text if text is not None else ""))


def _day(iso) -> str:
    y, m, d = str(iso).split("-")
    return f"{d}/{m}/{y}"


def build_estimate_document(conn, user_id, estimate_id, *, today=None) -> dict:
    """{'html', 'paper', 'file_name', 'estimate'} ready for ui.print_service. Changes nothing."""
    e = est.get_estimate(conn, user_id, estimate_id, today=today)
    lang = get_setting(conn, "bill_language") or "en"
    w = _WORDS.get(lang) or _WORDS["en"]
    spec = paper_spec(get_setting(conn, "bill_paper"))
    shop = get_shop_profile(conn, str(e["created_at"])[:10])
    size = spec["font_pt"]
    out = [f'<html><head><meta charset="utf-8"></head><body style="font-size:{size}pt;">']
    out.append(f'<p align="center"><b style="font-size:{size + 3}pt;">{_e(shop.get("name") or "")}</b>')
    for key in ("address", "phone"):
        if shop.get(key):
            out.append(f"<br>{_e(shop[key])}")
    out.append("</p>")
    out.append(f'<p align="center"><b>{_e(w["title"])}</b><br>{_e(w["not_invoice"])}')
    if e["expired"]:
        out.append(f'<br><b>*** {_e(w["expired"])} ***</b>')
    out.append("</p>")
    out.append('<table width="100%" cellspacing="0" cellpadding="1">')
    out.append(f'<tr><td>{_e(w["no"])} {_e(e["estimate_no"])}</td><td align="right">{_e(_when(e["created_at"]))}</td></tr>')
    out.append(f'<tr><td colspan="2">{_e(w["valid"])}: {_e(_day(e["valid_until"]))}</td></tr>')
    if e["customer_name"]:
        out.append(f'<tr><td colspan="2">{_e(w["customer"])}: {_e(e["customer_name"])}</td></tr>')
    out.append("</table><hr>")
    out.append('<table width="100%" cellspacing="0" cellpadding="1">')
    out.append(f'<tr><th align="left">{_e(w["item"])}</th><th align="right">{_e(w["qty"])}</th>'
               f'<th align="right">{_e(w["rate"])}</th><th align="right">{_e(w["amount"])}</th></tr>')
    for line in e["lines"]:
        out.append(f'<tr><td>{_e(line["label"])}</td><td align="right">{line["qty"]}</td>'
                   f'<td align="right">{_e(format_rupees(line["unit_price_paise"], symbol=""))}</td>'
                   f'<td align="right">{_e(format_rupees(line["line_total_paise"], symbol=""))}</td></tr>')
        if line["discount_paise"]:
            out.append(f'<tr><td colspan="4"><small>- {_e(format_rupees(line["discount_paise"], symbol=""))}</small></td></tr>')
    out.append("</table><hr>")
    out.append(f'<p align="right"><b>{_e(w["total"])}: {_e(format_rupees(e["total_paise"]))}</b></p>')
    if e["note"]:
        out.append(f'<p>{_e(w["note"])}: {_e(e["note"])}</p>')
    out.append(f'<p align="center"><small>{_e(w["prices"])}</small></p></body></html>')
    return {"html": "".join(out), "paper": spec, "file_name": e["estimate_no"].replace("/", "-") + ".pdf", "estimate": e}
