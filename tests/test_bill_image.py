"""BIL-11: a saved bill as a PNG picture. Content comes from build_bill; copies are marked and logged."""

import pytest
from PIL import Image

import core.bill_image as bi
from core import auth
from core import shop_settings as ss
from core.audit import verify_chain
from core.bill_document import build_bill
from core.guarded import sell
from core.permissions import PermissionDenied
from core.product_edit import add_product
from core.settings import set_setting
from core.stock import add_movement, get_stock
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

GSTIN = "27AAPFU0939F1ZV"


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    ss.update_shop_profile(conn, owner, name="Anu Cloth House", address="MG Road, Pune", phone="9876543210",
                           gstin=GSTIN, state_code="27", tax_scheme="regular")
    ensure_default_tax_rules(conn)
    shirt = add_product(conn, owner, "Cotton Shirt", sizes=["M"], colours=["Blue"], hsn="6205", default_price_paise=105000)
    blazer = add_product(conn, owner, "Blazer", sizes=["L"], colours=["Black"], hsn="6203", default_price_paise=413000)
    (v1,), (v2,) = shirt["variant_ids"], blazer["variant_ids"]
    with transaction(conn):
        add_movement(conn, v1, 20, "opening stock")
        add_movement(conn, v2, 20, "opening stock")
    customer = conn.execute("INSERT INTO customers(name, phone) VALUES ('Ravi Kumar', '9876512345')").lastrowid
    cart = [{"variant_id": v1, "qty": 2}, {"variant_id": v2, "qty": 1}]                 # the SRS example, Rs 6,230
    bill = sell(conn, cart, [{"mode": "upi", "amount_paise": 500000}, {"mode": "cash", "amount_paise": 150000}],
                user_id=till, customer_id=customer, mode="regular", intra_state=True)
    simple = sell(conn, cart[:1], [{"mode": "cash", "amount_paise": 210000}], user_id=till, mode="unregistered")
    yield type("S", (), {"conn": conn, "owner": owner, "till": till, "store": store, "bill": bill,
                         "simple": simple, "v1": v1, "folder": tmp_path})
    conn.close()


def _count(s, table, where=""):
    return s.conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def _reprints(s):
    return _count(s, "audit_log", "WHERE action = 'sale_reprint'")


def _picture(s, sale=None, **kwargs):
    return bi.render_bill_image(build_bill(s.conn, sale or s.bill), **kwargs)


def test_saves_a_real_png_and_the_first_picture_logs_nothing(s):
    before = _count(s, "audit_log")
    path = bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "bill.png"))
    assert path == str(s.folder / "bill.png")
    with Image.open(path) as image:
        assert image.format == "PNG" and image.width == 576 and image.height > 300
        assert image.convert("L").getextrema()[0] < 100                                 # something dark was drawn
    assert _count(s, "audit_log") == before and not list(s.folder.glob("*.part"))


def test_width_follows_the_paper_in_settings_or_the_given_width(s):
    for paper, width in (("thermal58", 384), ("thermal80", 576), ("a5", 874), ("a4", 1240)):
        set_setting(s.conn, "bill_paper", paper)
        assert _picture(s).width == width
    assert _picture(s, width_px=700).width == 700
    for bad in (299, 2001, 576.5, True):
        with pytest.raises(ValueError, match="width"):
            _picture(s, width_px=bad)


def test_the_picture_says_what_the_bill_document_says(s):
    set_setting(s.conn, "bill_footer", "Exchange only with this bill")
    plain = _picture(s)
    set_setting(s.conn, "bill_footer", "")
    assert plain.height > _picture(s).height                                             # the footer line is drawn
    assert _picture(s).height > _picture(s, sale=s.simple).height                        # tax rows, GSTIN, HSN only on a tax invoice
    set_setting(s.conn, "bill_paper", "a4")
    wide = _picture(s)
    assert wide.width == 1240 and wide.height > 300                                      # HSN and GST columns on sheets


def test_hindi_and_marathi_bills_are_drawn_with_their_own_words(s):
    english = _picture(s)
    for lang in ("hi", "mr"):
        set_setting(s.conn, "bill_language", lang)
        image = _picture(s)
        assert image.width == english.width and image.tobytes() != english.tobytes()
        assert image.convert("L").getextrema()[0] < 100


def test_a_copy_is_marked_logged_once_and_numbered(s):
    first = bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "a.png"))
    assert _reprints(s) == 0
    copy1 = bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "b.png"), duplicate=True)
    copy2 = bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "c.png"), duplicate=True)
    assert _reprints(s) == 2
    rows = s.conn.execute("SELECT user_id, entity_id, after_json FROM audit_log WHERE action = 'sale_reprint' ORDER BY id").fetchall()
    assert [(r["user_id"], r["entity_id"]) for r in rows] == [(s.till, s.bill)] * 2
    assert '"copy_number": 1' in rows[0]["after_json"] and '"copy_number": 2' in rows[1]["after_json"]
    with Image.open(first) as a, Image.open(copy1) as b, Image.open(copy2) as c:
        assert b.height > a.height and c.height > a.height                               # the DUPLICATE banner
    assert verify_chain(s.conn) == (True, None)


def test_saving_a_picture_never_changes_the_sale_stock_or_payments(s):
    before = (_count(s, "sales"), _count(s, "sale_lines"), _count(s, "payments"), _count(s, "stock_movements"),
              get_stock(s.conn, s.v1))
    bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "a.png"))
    bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "b.png"), duplicate=True)
    assert (_count(s, "sales"), _count(s, "sale_lines"), _count(s, "payments"), _count(s, "stock_movements"),
            get_stock(s.conn, s.v1)) == before


def test_a_bad_name_or_folder_leaves_no_file_and_no_log(s):
    with pytest.raises(ValueError, match=".png"):
        bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "bill.jpg"), duplicate=True)
    with pytest.raises(ValueError, match="folder does not exist"):
        bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "missing" / "bill.png"), duplicate=True)
    (s.folder / "taken.png").mkdir()                                                     # a folder where the file should go
    with pytest.raises(ValueError, match="could not be saved"):
        bi.save_bill_image(s.conn, s.till, s.bill, str(s.folder / "taken.png"), duplicate=True)
    assert _reprints(s) == 0 and not list(s.folder.glob("*.part"))


def test_if_the_copy_cannot_be_logged_the_file_is_removed_again(s, monkeypatch):
    real = bi.build_bill

    def failing(conn, sale_id, **kwargs):
        if kwargs.get("duplicate"):
            raise RuntimeError("database is locked")
        return real(conn, sale_id, **kwargs)

    monkeypatch.setattr(bi, "build_bill", failing)
    target = s.folder / "copy.png"
    with pytest.raises(ValueError, match="Nothing was changed") as err:
        bi.save_bill_image(s.conn, s.till, s.bill, str(target), duplicate=True)
    assert "locked" not in str(err.value) and not target.exists() and _reprints(s) == 0


def test_unknown_bill_and_missing_permission_are_refused(s):
    with pytest.raises(ValueError, match="Bill not found"):
        bi.save_bill_image(s.conn, s.till, 999, str(s.folder / "x.png"))
    with pytest.raises(PermissionDenied, match="not allowed to save bills"):
        bi.save_bill_image(s.conn, s.store, s.bill, str(s.folder / "x.png"))
    assert not (s.folder / "x.png").exists()