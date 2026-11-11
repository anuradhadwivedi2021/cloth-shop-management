"""BIL-13: a static UPI QR on the bill and at the counter, from the shop's own UPI id. The cashier confirms payment."""

import io
import os
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import upi_qr as uq  # noqa: E402
from core.bill_document import bill_html, build_bill  # noqa: E402
from core.customers import create_customer  # noqa: E402
from core.sales import create_sale  # noqa: E402
from core.settings import set_setting  # noqa: E402
from core.shop_settings import update_shop_profile  # noqa: E402
from tests.test_bill_document import DAY, _cart, _sell, shop  # noqa: E402,F401  (the same small shop)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


def _decode_png(png: bytes) -> str:
    image = np.array(Image.open(io.BytesIO(png)).convert("L"))
    return cv2.QRCodeDetector().detectAndDecode(image)[0]


def _decode_array(array) -> str:
    return cv2.QRCodeDetector().detectAndDecode(array)[0]


# ---------------------------------------------------------------- the code itself

def test_the_link_has_the_shop_id_name_and_amount():
    link = uq.upi_link("anu.shop@okaxis", "Anu Cloth House", 105050, "INV/26-27/000012")
    assert link == "upi://pay?pa=anu.shop@okaxis&pn=Anu%20Cloth%20House&am=1050.50&cu=INR&tn=INV%2F26-27%2F000012"
    assert uq.upi_link("ab@b1", "Shop") == "upi://pay?pa=ab@b1&pn=Shop&cu=INR"       # no amount: the customer types it
    assert "am=0.05" in uq.upi_link("ab@b1", "Shop", 5) and "am=7.00" in uq.upi_link("ab@b1", "Shop", 700)


@pytest.mark.parametrize("bad", ["", None, "nohandle", "a@", "@bank", "has space@bank", "a@@bank", "x@1bank"])
def test_a_bad_upi_id_is_refused(bad):
    assert uq.is_valid_upi_id(bad) is False
    with pytest.raises(ValueError, match="UPI id"):
        uq.upi_link(bad, "Shop")


@pytest.mark.parametrize("bad", [0, -5, True, 12.5, "100", 10**12])
def test_a_bad_amount_is_refused(bad):
    with pytest.raises(ValueError):
        uq.upi_link("ab@bank", "Shop", bad)


def test_the_qr_picture_really_scans_back_to_the_link():
    for amount in (None, 105050, 5, 623000):
        link = uq.upi_link("anu.shop@okaxis", "Anu Cloth House — Pune", amount, "Bill 12")
        assert _decode_png(uq.qr_png(link)) == link
        assert _decode_png(uq.qr_png(link, scale=3)) == link


def test_the_picture_is_a_clean_square_with_a_quiet_border():
    link = uq.upi_link("ab@bank", "Shop", 100)
    matrix = uq.qr_matrix(link)
    assert len(matrix) == len(matrix[0]) >= 21
    png = uq.qr_png(link, scale=5, border=4)
    image = Image.open(io.BytesIO(png))
    assert image.size == ((len(matrix) + 8) * 5,) * 2 and image.mode == "1"
    pixels = np.array(image.convert("L"))
    assert pixels[:20].min() == 255 and pixels[:, :20].min() == 255        # the border is white


def test_picture_and_text_limits():
    with pytest.raises(ValueError):
        uq.qr_png("")
    with pytest.raises(ValueError):
        uq.qr_png("x" * 2000)
    with pytest.raises(ValueError):
        uq.qr_png("x", scale=0)


def test_no_valid_upi_id_means_no_qr():
    assert uq.qr_for_bill({"upi_id": None, "name": "S"}, 100) is None
    assert uq.qr_for_bill({"upi_id": "bad", "name": "S"}, 100) is None
    assert uq.qr_for_bill({}, 100) is None
    got = uq.qr_for_bill({"upi_id": " ab@bank ", "name": "S"}, 100)
    assert got["upi_id"] == "ab@bank" and got["link"].startswith("upi://pay?pa=ab@bank") and got["png"][:4] == b"\x89PNG"


# ---------------------------------------------------------------- on the bill

def _credit_bill(s, paid=100000):
    set_setting(s.conn, "credit_sales_enabled", "1")
    cust = create_customer(s.conn, "9000000002", user_id=1, name="Sita", consent_given=True)
    sid = create_sale(s.conn, _cart(s), [{"mode": "cash", "amount_paise": paid}], user_id=1, on_date=DAY, customer_id=cust)
    return build_bill(s.conn, sid)


def test_a_bill_with_an_amount_still_to_pay_carries_the_qr_for_exactly_that_amount(shop):
    update_shop_profile(shop.conn, 1, upi_id="anu.shop@okaxis", now=datetime(2026, 1, 2))
    bill = _credit_bill(shop)
    assert bill["upi"]["amount_paise"] == bill["due_paise"] == 623000 - 100000
    assert f"am={bill['due_paise'] // 100}.00" in bill["upi"]["link"] and "anu.shop@okaxis" in bill["upi"]["link"]
    html = bill_html(bill)
    assert "Scan to pay by UPI" in html and "data:image/png;base64," in html and "anu.shop@okaxis" in html
    assert _decode_png(bill["upi"]["png"]) == bill["upi"]["link"]


def test_a_fully_paid_bill_has_no_qr(shop):
    update_shop_profile(shop.conn, 1, upi_id="anu.shop@okaxis", now=datetime(2026, 1, 2))
    bill = build_bill(shop.conn, _sell(shop))
    assert bill["upi"] is None and "data:image" not in bill_html(bill)


def test_no_qr_when_the_setting_is_off_or_the_shop_has_no_upi_id(shop):
    bill = _credit_bill(shop)                                    # no UPI id in this shop yet
    assert bill["upi"] is None and "Scan to pay" not in bill_html(bill)
    update_shop_profile(shop.conn, 1, upi_id="anu.shop@okaxis", now=datetime(2026, 1, 2))
    set_setting(shop.conn, "show_upi_qr_on_bill", "0")
    assert build_bill(shop.conn, _credit_bill_id(shop))["upi"] is None


def _credit_bill_id(s):
    cust = create_customer(s.conn, "9000000003", user_id=1, name="Gita", consent_given=True)
    return create_sale(s.conn, _cart(s), [{"mode": "cash", "amount_paise": 1000}], user_id=1, on_date=DAY, customer_id=cust)


def test_the_qr_in_the_printed_html_really_draws_and_scans(shop):
    """Qt's text engine must show the embedded picture: render the bill and read the QR back out of it."""
    from PySide6.QtGui import QColor, QImage, QPainter, QTextDocument
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    update_shop_profile(shop.conn, 1, upi_id="anu.shop@okaxis", now=datetime(2026, 1, 2))
    bill = _credit_bill(shop)
    doc = QTextDocument()
    doc.setHtml(bill_html(bill))
    doc.setTextWidth(500)
    image = QImage(500, int(doc.size().height()) + 10, QImage.Format_Grayscale8)
    image.fill(QColor("white"))
    painter = QPainter(image)
    doc.drawContents(painter)
    painter.end()
    ptr = image.constBits()
    array = np.frombuffer(ptr, dtype=np.uint8).reshape(image.height(), image.bytesPerLine())[:, :500].copy()
    assert _decode_array(array) == bill["upi"]["link"]


def test_the_shared_bill_picture_carries_the_qr_too(shop):
    from core.bill_image import render_bill_image

    update_shop_profile(shop.conn, 1, upi_id="anu.shop@okaxis", now=datetime(2026, 1, 2))
    bill = _credit_bill(shop)
    picture = render_bill_image(bill)
    assert _decode_array(np.array(picture.convert("L"))) == bill["upi"]["link"]
    paid = render_bill_image(build_bill(shop.conn, _sell(shop)))
    assert _decode_array(np.array(paid.convert("L"))) == ""


def test_an_old_bill_reprint_uses_the_amount_then_still_due(shop):
    update_shop_profile(shop.conn, 1, upi_id="anu.shop@okaxis", now=datetime(2026, 1, 2))
    set_setting(shop.conn, "credit_sales_enabled", "1")
    cust = create_customer(shop.conn, "9000000004", user_id=1, name="Rita", consent_given=True)
    sid = create_sale(shop.conn, _cart(shop), [{"mode": "cash", "amount_paise": 123400}], user_id=1, on_date=DAY, customer_id=cust)
    first = build_bill(shop.conn, sid)
    again = build_bill(shop.conn, sid, user_id=1, duplicate=True)
    assert first["upi"]["link"] == again["upi"]["link"]


# ---------------------------------------------------------------- at the counter

def test_the_payment_dialog_shows_and_hides_the_qr_and_never_marks_anything_paid(shop):
    from PySide6.QtWidgets import QApplication

    from ui.billing_counter_view import PaymentDialog

    QApplication.instance() or QApplication([])
    seen = []
    plain = PaymentDialog(623000, lambda p: seen.append(p) or (True, ""), upi=None)
    assert plain.upi_btn.isHidden() and seen == []

    update_shop_profile(shop.conn, 1, upi_id="anu.shop@okaxis", now=datetime(2026, 1, 2))
    qr = uq.qr_for_bill(__import__("core.shop_settings", fromlist=["x"]).get_shop_profile(shop.conn), 623000)
    dialog = PaymentDialog(623000, lambda p: seen.append(p) or (True, ""), upi=qr)
    dialog.show()
    assert dialog.upi_btn.isVisible() and not dialog.upi_label.isVisible()
    dialog.upi_btn.click()
    assert dialog.upi_label.isVisible() and not dialog.upi_label.pixmap().isNull()
    assert "6,230.00" in dialog.upi_note.text() and "anu.shop@okaxis" in dialog.upi_note.text()
    assert seen == []                                            # showing the QR records no payment
    dialog.upi_btn.click()
    assert not dialog.upi_label.isVisible()
    dialog.close()
