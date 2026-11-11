"""Barcode labels (SRS STK-05, PUR-05): what a label says, the Code 128 bars, printing and the Stock screen button.

The real printer is replaced (a test machine has none); the label drawing, the PDF file and every
message are real. The bars are also checked from the PICTURE: draw a label, read the black and white
runs back out of the image and compare them with the bars core/labels.py produced.
"""

import json
import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import labels, shop  # noqa: E402
from core.permissions import PermissionDenied  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.shop_settings import update_preferences, update_shop_profile  # noqa: E402
from core.stock import add_movement  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import i18n, print_service  # noqa: E402
from ui.screens.stock_screen import StockScreen  # noqa: E402

_APP = QApplication.instance() or QApplication([])
EAN = "2000000000015"


@pytest.fixture
def world(tmp_path):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    shop.add_user(folder, conn, owner, "store", "store-password", "stock")
    shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    update_shop_profile(conn, owner.user_id, name="Anu Cloth House", tax_scheme="unregistered")
    shirt = create_product(conn, "Cotton Shirt", hsn="6205", default_price_paise=105000, mrp_paise=120000)
    v_m, v_l = generate_variants(conn, shirt, ["M", "L"], ["Blue"])
    with transaction(conn):
        add_movement(conn, v_m, 5, "opening stock")
        add_movement(conn, v_l, 5, "opening stock")
    conn.close()
    yield folder, v_m, v_l
    i18n.set_language("en")


def _open(folder, user="owner"):
    password = "owner-password" if user == "owner" else f"{user}-password"
    return shop.open_shop(folder, user, password)


# ---------------------------------------------------------------- the bars

def test_ean13_style_code_uses_the_compact_code_c():
    bars = labels.barcode_bars(EAN)
    assert sum(bars) == 123                       # start C + 6 pairs + switch + 1 + check + stop
    assert bars[:6] == [2, 1, 1, 2, 3, 2]         # the Code 128 "start C" pattern 211232
    assert bars[-7:] == [2, 3, 3, 1, 1, 1, 2]     # the stop pattern 2331112
    assert len(bars) % 2 == 1                     # begins and ends with a bar


def test_text_code_uses_code_b_and_has_the_right_width():
    bars = labels.barcode_bars("SHIRT-M-BLUE")     # 12 characters
    assert bars[:6] == [2, 1, 1, 2, 1, 4]         # "start B" 211214
    assert sum(bars) == 11 * (12 + 2) + 13        # start + 12 + check = 14 symbols of 11, plus the 13-wide stop
    assert all(1 <= width <= 4 for width in bars)


@pytest.mark.parametrize("bad", ["", None, "Rs ₹100", "tab\there"])
def test_a_barcode_that_cannot_be_encoded_is_refused(bad):
    with pytest.raises(ValueError):
        labels.barcode_bars(bad)


# ---------------------------------------------------------------- what a label says

def test_label_shows_shop_product_size_colour_price_mrp_and_barcode(world):
    folder, v_m, _ = world
    conn, session = _open(folder)
    job = labels.build_labels(conn, session.user_id, [{"variant_id": v_m, "copies": 3}])
    assert (job["size"], job["width_mm"], job["height_mm"], job["total"]) == ("50x25", 50.0, 25.0, 3)
    (label,) = job["labels"]
    assert label["shop"] == "Anu Cloth House" and label["product"] == "Cotton Shirt"
    assert (label["size"], label["colour"], label["copies"]) == ("M", "Blue", 3)
    assert (label["price_paise"], label["mrp_paise"]) == (105000, 120000)
    assert len(label["barcode"]) == 13 and label["barcode"].isdigit()
    assert label["bars"] == labels.barcode_bars(label["barcode"])
    assert "cost" not in " ".join(label)      # cost price is never on a tag


def test_the_variants_own_price_wins_over_the_product_price(world):
    folder, v_m, _ = world
    conn, session = _open(folder)
    conn.execute("UPDATE variants SET price_override_paise = 99900 WHERE id = ?", (v_m,))
    conn.commit()
    (label,) = labels.build_labels(conn, session.user_id, [{"variant_id": v_m, "copies": 1}])["labels"]
    assert label["price_paise"] == 99900


def test_a_variant_without_a_barcode_falls_back_to_its_sku(world):
    folder, v_m, _ = world
    conn, session = _open(folder)
    sku = conn.execute("SELECT sku FROM variants WHERE id = ?", (v_m,)).fetchone()[0]
    conn.execute("UPDATE variants SET barcode = NULL WHERE id = ?", (v_m,))
    conn.commit()
    (label,) = labels.build_labels(conn, session.user_id, [{"variant_id": v_m, "copies": 1}])["labels"]
    assert label["barcode"] == sku


def test_settings_choose_the_size_and_what_is_printed(world):
    folder, v_m, _ = world
    conn, session = _open(folder)
    update_preferences(conn, session.user_id,
                       {"label_size": "38x25", "label_show_shop_name": "0", "label_show_mrp": "0"})
    job = labels.build_labels(conn, session.user_id, [{"variant_id": v_m, "copies": 1}])
    assert (job["width_mm"], job["height_mm"]) == (38.0, 25.0)
    assert job["labels"][0]["shop"] == "" and job["labels"][0]["mrp_paise"] is None


def test_the_same_variant_twice_is_added_together(world):
    folder, v_m, v_l = world
    conn, session = _open(folder)
    job = labels.build_labels(conn, session.user_id, [{"variant_id": v_m, "copies": 2},
                                                       {"variant_id": v_l, "copies": 1},
                                                       {"variant_id": v_m, "copies": 3}])
    assert {l["variant_id"]: l["copies"] for l in job["labels"]} == {v_m: 5, v_l: 1} and job["total"] == 6


@pytest.mark.parametrize("items, message", [
    ([], "at least one item"),
    (None, "at least one item"),
    ([{"copies": 1}], "at least one item"),
    ([{"variant_id": 1, "copies": 0}], "at least 1"),
    ([{"variant_id": 1, "copies": True}], "at least 1"),
    ([{"variant_id": 1, "copies": 501}], "At most 500"),
    ([{"variant_id": 1, "copies": 300}, {"variant_id": 1, "copies": 300}], "At most 500"),
    ([{"variant_id": 999999, "copies": 1}], "could not be found"),
])
def test_bad_requests_are_refused_with_a_plain_message(world, items, message):
    conn, session = _open(world[0])
    with pytest.raises(ValueError, match=message):
        labels.build_labels(conn, session.user_id, items)


def test_one_job_has_a_maximum(world):
    folder, *_ = world
    conn, session = _open(folder)
    shirt = create_product(conn, "Kurta", hsn="6205", default_price_paise=50000)
    many = generate_variants(conn, shirt, ["S", "M", "L", "XL", "XXL"], ["Red"])
    with pytest.raises(ValueError, match="At most 2000"):
        labels.build_labels(conn, session.user_id, [{"variant_id": v, "copies": 500} for v in many])


def test_only_users_with_the_permission_can_build_labels(world):
    folder, v_m, _ = world
    items = [{"variant_id": v_m, "copies": 1}]
    conn, session = _open(folder, "till")
    with pytest.raises(PermissionDenied):
        labels.build_labels(conn, session.user_id, items)
    conn, session = _open(folder, "store")
    assert labels.build_labels(conn, session.user_id, items)["total"] == 1


# ---------------------------------------------------------------- the picture

def _runs_from_picture(job_label, width_mm, height_mm, dpmm=24):
    """Draw the label, read one pixel row through the bars and return the run lengths in modules."""
    img = QImage(int(width_mm * dpmm), int(height_mm * dpmm), QImage.Format_RGB32)
    img.fill(QColor("white"))
    painter = QPainter(img)
    print_service.draw_label(painter, job_label, width_mm, height_mm, dpmm)
    painter.end()
    y = int((height_mm - 1.2 - 2.4 - 2.0) * dpmm)          # inside the bars, above the digits
    row = [img.pixelColor(x, y).lightness() < 128 for x in range(img.width())]
    runs, start = [], 0
    for x in range(1, len(row) + 1):
        if x == len(row) or row[x] != row[start]:
            runs.append((row[start], x - start))
            start = x
    return img, [(black, n) for black, n in runs]


@pytest.mark.parametrize("size", ["50x25", "38x25"])
def test_the_drawn_bars_are_exactly_the_bars_the_core_made(world, size):
    folder, v_m, _ = world
    conn, session = _open(folder)
    update_preferences(conn, session.user_id, {"label_size": size})
    job = labels.build_labels(conn, session.user_id, [{"variant_id": v_m, "copies": 1}])
    label = job["labels"][0]
    img, runs = _runs_from_picture(label, job["width_mm"], job["height_mm"])
    bar_runs = [n for black, n in runs]
    if not runs[0][0]:
        bar_runs = bar_runs[1:]                      # the blank quiet zone before the first bar
    if not runs[-1][0]:
        bar_runs = bar_runs[:-1]                     # and after the last one
    module_px = min(bar_runs)
    drawn = [round(n / module_px) for n in bar_runs]
    assert drawn == label["bars"]
    assert module_px >= 0.18 * 24 - 1                # printable: at least ~0.18 mm per module


def test_a_long_product_name_never_runs_off_the_label(world):
    folder, v_m, _ = world
    conn, session = _open(folder)
    (label,) = labels.build_labels(conn, session.user_id, [{"variant_id": v_m, "copies": 1}])["labels"]
    label = {**label, "product": "Extra Long Cotton Blend Slim Fit Formal Shirt With Contrast Collar " * 3}
    img, _ = _runs_from_picture(label, 38.0, 25.0)
    # nothing but white in the 1 mm margin at the left and right edges
    margin = int(1.0 * 24)
    for x in list(range(margin)) + list(range(img.width() - margin, img.width())):
        assert all(img.pixelColor(x, y).lightness() > 200 for y in range(img.height()))


# ---------------------------------------------------------------- printing and PDF

@pytest.fixture
def printer(monkeypatch):
    seen = {"jobs": [], "fail": None, "name": "Label Printer"}

    def fake(job):
        if seen["fail"]:
            raise seen["fail"]
        seen["jobs"].append(job)
        return seen["name"]

    monkeypatch.setattr(print_service, "_print_labels", fake)
    return seen


def _audit(conn):
    return [dict(r) for r in conn.execute("SELECT action, after_json FROM audit_log WHERE action = 'labels_printed'")]


def test_printing_sends_the_job_and_writes_one_audit_entry(world, printer):
    folder, v_m, _ = world
    conn, session = _open(folder)
    result = print_service.print_labels(conn, session, [{"variant_id": v_m, "copies": 4}])
    assert result.ok and "4 label(s)" in result.message and "Label Printer" in result.message
    assert printer["jobs"][0]["total"] == 4
    (entry,) = _audit(conn)
    assert json.loads(entry["after_json"])["labels"] == 4


@pytest.mark.parametrize("error, text", [
    (RuntimeError("no printer"), "No printer was found"),
    (OSError("spooler down"), "printer reported a problem"),
])
def test_a_printer_problem_is_reported_and_nothing_is_recorded(world, printer, error, text):
    folder, v_m, _ = world
    conn, session = _open(folder)
    printer["fail"] = error
    result = print_service.print_labels(conn, session, [{"variant_id": v_m, "copies": 1}])
    assert not result.ok and text in result.message
    assert _audit(conn) == []


def test_a_user_without_permission_cannot_print(world, printer):
    folder, v_m, _ = world
    conn, session = _open(folder, "till")
    result = print_service.print_labels(conn, session, [{"variant_id": v_m, "copies": 1}])
    assert not result.ok and "not allowed" in result.message and printer["jobs"] == []


def test_bad_input_gives_a_message_not_a_crash(world, printer):
    conn, session = _open(world[0])
    result = print_service.print_labels(conn, session, [])
    assert not result.ok and "at least one item" in result.message and printer["jobs"] == []


def test_a_locked_session_cannot_print(world, printer):
    folder, v_m, _ = world
    conn, session = _open(folder)
    session.lock()
    result = print_service.print_labels(conn, session, [{"variant_id": v_m, "copies": 1}])
    assert not result.ok and printer["jobs"] == []


def test_the_pdf_has_one_page_per_label(world, tmp_path):
    folder, v_m, v_l = world
    conn, session = _open(folder)
    path = str(tmp_path / "out" / "labels.pdf")
    result = print_service.save_labels_pdf(conn, session, [{"variant_id": v_m, "copies": 3},
                                                           {"variant_id": v_l, "copies": 2}], path)
    assert result.ok and result.path == path
    data = open(path, "rb").read()
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page(?![s\w])", data)) == 5
    # 50 x 25 mm in PDF points (1 pt = 1/72 inch)
    box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)", data)
    assert abs(float(box.group(1)) - 50 / 25.4 * 72) < 1 and abs(float(box.group(2)) - 25 / 25.4 * 72) < 1


def test_labels_follow_the_screen_language(world, printer):
    folder, v_m, _ = world
    conn, session = _open(folder)
    i18n.set_language("hi")
    result = print_service.print_labels(conn, session, [{"variant_id": v_m, "copies": 2}])
    assert "लेबल" in result.message


# ---------------------------------------------------------------- the Stock and Variants screen

def test_stock_screen_prints_labels_for_the_selected_rows(world, printer):
    folder, v_m, v_l = world
    conn, session = _open(folder, "store")
    screen = StockScreen(conn, session)
    assert screen.btn_labels.isEnabled()
    screen.copies.setValue(2)
    screen.table.selectAll()
    screen.print_labels()
    job = printer["jobs"][0]
    assert {l["variant_id"]: l["copies"] for l in job["labels"]} == {v_m: 2, v_l: 2}
    assert "4 label(s)" in screen.message.text()


def test_stock_screen_asks_for_a_selection_first(world, printer):
    conn, session = _open(world[0], "store")
    screen = StockScreen(conn, session)
    screen.table.clearSelection()
    screen.print_labels()
    assert printer["jobs"] == [] and "Select the items" in screen.message.text()


def test_stock_screen_saves_a_pdf_to_the_chosen_file(world, tmp_path, monkeypatch):
    conn, session = _open(world[0])
    screen = StockScreen(conn, session)
    target = str(tmp_path / "tags.pdf")
    monkeypatch.setattr("ui.screens.stock_screen.QFileDialog.getSaveFileName", lambda *a, **k: (target, ""))
    screen.table.selectRow(0)
    screen.save_labels_pdf()
    assert os.path.getsize(target) > 0 and "saved as a PDF" in screen.message.text()


def test_stock_screen_buttons_are_off_for_a_user_without_permission(world, printer):
    conn, session = _open(world[0], "till")
    screen = StockScreen(conn, session)
    assert not screen.btn_labels.isEnabled() and not screen.btn_labels_pdf.isEnabled()
