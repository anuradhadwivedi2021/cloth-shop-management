"""PRD-11: fabric sold by the metre."""

import pytest

from core import fabric
from core.bill_preview import preview_bill, variant_info
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.sales import create_sale
from core.auth import create_user
from tests.test_bill_document import DAY, shop  # noqa: F401


@pytest.fixture
def cloth(shop):
    pid = create_product(shop.conn, "Cotton cloth", hsn="6205", default_price_paise=25000, unit="metre")   # Rs 250 / m
    (vid,) = generate_variants(shop.conn, pid, ["Free"], ["White"])
    shop.pid, shop.vid = pid, vid
    return shop


def test_metres_and_centimetres():
    assert fabric.parse_metres("2.5") == 250 and fabric.parse_metres("0,75 m") == 75
    for bad in ("0", "-1", "abc", "1.234", "20000"):
        with pytest.raises(ValueError):
            fabric.parse_metres(bad)
    assert [fabric.format_metres(c) for c in (250, 100, 5, 125)] == ["2.5 m", "1 m", "0.05 m", "1.25 m"]
    assert fabric.qty_text("piece", 3) == "3" and fabric.qty_text("metre", 250) == "2.5 m"


def test_price_per_metre_must_be_whole_rupees():
    assert fabric.cm_price("metre", 25000) == 250 and fabric.cm_price("piece", 25000) == 25000
    with pytest.raises(ValueError):
        fabric.cm_price("metre", 25050)


def test_receive_roll_sell_a_cut_and_remnant_goes_first(cloth):
    s = cloth
    fabric.receive_piece(s.conn, 1, s.vid, "roll", "20", ref="R1")
    fabric.receive_piece(s.conn, 1, s.vid, "remnant", "1.5")
    assert variant_info(s.conn, s.vid)["sellable"] == 2150
    assert variant_info(s.conn, s.vid)["price_paise"] == 250                 # one centimetre
    sid = create_sale(s.conn, [{"variant_id": s.vid, "qty": 250}], [{"mode": "cash", "amount_paise": 62500}],
                      user_id=1, on_date=DAY)                                # 2.5 m x Rs 250 = Rs 625
    assert s.conn.execute("SELECT total_paise FROM sales WHERE id = ?", (sid,)).fetchone()[0] == 62500
    pieces = {p["kind"]: p for p in fabric.list_pieces(s.conn, 1, only_open=False)}
    assert pieces["remnant"]["remaining_cm"] == 0 and pieces["roll"]["remaining_cm"] == 2000 - 100
    assert variant_info(s.conn, s.vid)["sellable"] == 1900


def test_minimum_cut_is_enforced(cloth):
    s = cloth
    fabric.receive_piece(s.conn, 1, s.vid, "roll", "10")
    assert fabric.set_min_cut(s.conn, 1, s.pid, "0.5") == 50
    with pytest.raises(ValueError, match="shortest cut"):
        create_sale(s.conn, [{"variant_id": s.vid, "qty": 30}], [{"mode": "cash", "amount_paise": 750}], user_id=1, on_date=DAY)
    create_sale(s.conn, [{"variant_id": s.vid, "qty": 50}], [{"mode": "cash", "amount_paise": 12500}], user_id=1, on_date=DAY)


def test_checks_and_permissions(cloth):
    s = cloth
    other = create_product(s.conn, "Shirt2", hsn="6205", default_price_paise=1000)
    (ov,) = generate_variants(s.conn, other, ["M"], ["Red"])
    with pytest.raises(ValueError):
        fabric.receive_piece(s.conn, 1, ov, "roll", "5")                    # not sold by the metre
    with pytest.raises(ValueError):
        fabric.receive_piece(s.conn, 1, s.vid, "bale", "5")
    cashier = create_user(s.conn, 1, "cash1", "Pass#12345", "cashier")
    with pytest.raises(PermissionDenied):
        fabric.receive_piece(s.conn, cashier, s.vid, "roll", "5")
    assert [p["name"] for p in fabric.metre_products(s.conn, 1)] == ["Cotton cloth"]
