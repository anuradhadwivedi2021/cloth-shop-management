"""The printed bill: every figure and word comes from the saved sale, nothing is changed by printing."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from core.bill_document import PAPERS, bill_file_name, bill_html, build_bill, paper_spec
from core.customers import create_customer
from core.products import create_product, generate_variants
from core.sales import create_sale
from core.settings import set_setting
from core.shop_settings import update_shop_profile
from core.stock import add_movement, get_stock
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction
from tests.test_shop_settings import KA, MH

DAY = "2026-11-01"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    ensure_default_tax_rules(conn)
    update_shop_profile(conn, 1, name="Anu Cloth House", address="MG Road, Pune", phone="9876543210",
                        gstin=MH, now=datetime(2026, 1, 1))
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)    # Rs 1,050
    blazer = create_product(conn, "Blazer", hsn="6202", default_price_paise=413000)  # Rs 4,130
    (m_blue,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    (l_black,) = generate_variants(conn, blazer, ["L"], ["Black"])
    with transaction(conn):
        for v in (m_blue, l_black):
            add_movement(conn, v, 10, "opening stock")
    yield SimpleNamespace(conn=conn, user=1, m_blue=m_blue, l_black=l_black)
    conn.close()


def _cart(s):
    return [{"variant_id": s.m_blue, "qty": 2}, {"variant_id": s.l_black, "qty": 1}]  # the SRS example: Rs 6,230


def _sell(s, total=623000, **kwargs):
    return create_sale(s.conn, _cart(s), [{"mode": "cash", "amount_paise": total}], user_id=s.user,
                       on_date=DAY, **kwargs)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- content

def test_tax_invoice_shows_shop_items_gst_split_and_total(shop):
    set_setting(shop.conn, "bill_paper", "a4")
    bill = build_bill(shop.conn, _sell(shop))
    html = bill_html(bill)
    assert bill["title"] == "TAX INVOICE" and bill["invoice_no"].startswith("INV")
    for text in ("Anu Cloth House", "MG Road, Pune", MH, "Shirt M Blue", "Blazer L Black", "6205", "6202",
                 "CGST", "SGST", "₹6,230.00", "Prices include GST", "Thank you! Please visit again."):
        assert text in html, text
    assert "IGST" not in html and "Duplicate" not in html
    # the money adds up on paper exactly as it was saved
    assert bill["taxable_paise"] + bill["tax_paise"] + bill["round_off_paise"] == bill["total_paise"] == 623000
    assert bill["cgst_paise"] + bill["sgst_paise"] == bill["tax_paise"]
    assert sum(b["taxable_paise"] for b in bill["tax_summary"]) == bill["taxable_paise"]
    assert {b["rate_percent"] for b in bill["tax_summary"]} == {5, 18}


def test_other_state_customer_gets_igst_not_cgst_sgst(shop):
    html = bill_html(build_bill(shop.conn, _sell(shop, intra_state=False)))
    assert "IGST" in html and "CGST" not in html and "SGST" not in html


def test_bill_of_supply_and_simple_bill_have_no_tax_columns(shop):
    update_shop_profile(shop.conn, 1, tax_scheme="composition", now=datetime(2026, 2, 1))
    comp = build_bill(shop.conn, _sell(shop, mode="composition"))
    html = bill_html(comp)
    assert comp["title"] == "BILL OF SUPPLY" and "Composition taxable person" in html
    assert "CGST" not in html and "HSN" not in html and "Taxable" not in html

    plain = build_bill(shop.conn, _sell(shop, mode="unregistered"))
    assert plain["title"] == "BILL" and "GSTIN" not in bill_html(plain)


def test_discount_round_off_customer_and_payments_are_shown(shop):
    cust = create_customer(shop.conn, "9000000001", user_id=1, name="Ravi Kumar", consent_given=True)
    sid = create_sale(shop.conn, _cart(shop), [{"mode": "cash", "amount_paise": 421000},
                                               {"mode": "upi", "amount_paise": 200000}],
                      user_id=1, on_date=DAY, bill_discount_paise=2000, customer_id=cust)
    bill = build_bill(shop.conn, sid)
    html = bill_html(bill)
    assert "Ravi Kumar" in html and "9000000001" in html
    assert bill["discount_paise"] == 2000 and "-₹20.00" in html
    assert [p["label"] for p in bill["payments"]] == ["Cash", "UPI"]
    assert bill["paid_paise"] == bill["total_paise"] and bill["due_paise"] == 0 and "BALANCE DUE" not in html
    if bill["round_off_paise"]:
        assert "Round off" in html


def test_cash_change_and_balance_due_are_shown(shop):
    sid = create_sale(shop.conn, _cart(shop), [{"mode": "cash", "amount_paise": 700000}], user_id=1, on_date=DAY)
    bill = build_bill(shop.conn, sid)
    assert bill["change_paise"] == 700000 - 623000 and bill["paid_paise"] == 623000
    assert "Change returned" in bill_html(bill)

    set_setting(shop.conn, "credit_sales_enabled", "1")
    cust = create_customer(shop.conn, "9000000002", user_id=1, name="Sita", consent_given=True)
    sid = create_sale(shop.conn, _cart(shop), [{"mode": "cash", "amount_paise": 100000}], user_id=1,
                      on_date=DAY, customer_id=cust)
    bill = build_bill(shop.conn, sid)
    assert bill["due_paise"] == bill["total_paise"] - 100000
    assert "BALANCE DUE" in bill_html(bill)


def test_footer_and_return_policy_follow_settings(shop):
    set_setting(shop.conn, "bill_footer", "Alterations free within 7 days")
    set_setting(shop.conn, "return_window_days", "15")
    set_setting(shop.conn, "final_sale_categories", "Innerwear, Sale items")
    html = bill_html(build_bill(shop.conn, _sell(shop)))
    assert "Alterations free within 7 days" in html
    assert "Returns accepted within 15 days with this bill." in html
    assert "Not returnable: Innerwear, Sale items." in html

    set_setting(shop.conn, "show_return_policy_on_bill", "0")
    assert "Returns accepted" not in bill_html(build_bill(shop.conn, _sell(shop)))


def test_text_from_the_database_cannot_break_the_page(shop):
    set_setting(shop.conn, "bill_footer", "<script>x</script> & more")
    html = bill_html(build_bill(shop.conn, _sell(shop)))
    assert "<script>" not in html and "&lt;script&gt;x&lt;/script&gt; &amp; more" in html


def test_an_old_bill_keeps_the_shop_details_of_its_own_date(shop):
    sid = _sell(shop)
    update_shop_profile(shop.conn, 1, name="New Name Traders", now=datetime(2099, 1, 1))
    assert build_bill(shop.conn, sid)["shop"]["name"] == "Anu Cloth House"


# ---------------------------------------------------------------- languages

@pytest.mark.parametrize("lang, title, total", [("hi", "टैक्स इनवॉइस", "कुल"), ("mr", "कर बीजक", "एकूण")])
def test_bill_words_follow_the_bill_language_setting(shop, lang, title, total):
    set_setting(shop.conn, "bill_language", lang)
    bill = build_bill(shop.conn, _sell(shop))
    html = bill_html(bill)
    assert bill["language"] == lang and title in html and total in html
    assert "TAX INVOICE" not in html and "₹6,230.00" in html


def test_every_language_has_the_same_words(shop):
    from core.bill_document import _TEXT
    assert set(_TEXT["hi"]) == set(_TEXT["en"]) == set(_TEXT["mr"])


def test_unknown_bill_language_falls_back_to_english(shop):
    set_setting(shop.conn, "bill_language", "xx")
    assert build_bill(shop.conn, _sell(shop))["language"] == "en"


# ---------------------------------------------------------------- paper

def test_paper_sizes_and_layouts(shop):
    sid = _sell(shop)
    assert paper_spec("thermal58")["width_mm"] == 58 and paper_spec("thermal80")["width_mm"] == 80
    assert paper_spec("a4")["height_mm"] == 297 and paper_spec("a5")["width_mm"] == 148
    assert paper_spec("nonsense")["name"] == "thermal80"
    assert paper_spec("thermal80")["roll"] and not paper_spec("a4")["roll"]

    for paper in PAPERS:
        set_setting(shop.conn, "bill_paper", paper)
        html = bill_html(build_bill(shop.conn, sid))
        assert "Shirt M Blue" in html and "₹6,230.00" in html
        assert ("<th" in html) == (not PAPERS[paper]["roll"])  # a table with headings only on A4 / A5
    set_setting(shop.conn, "bill_paper", "thermal80")
    assert "(HSN 6205" in bill_html(build_bill(shop.conn, sid))      # 80 mm roll: HSN and GST rate under each item
    set_setting(shop.conn, "bill_paper", "thermal58")
    assert "(HSN" not in bill_html(build_bill(shop.conn, sid))       # 58 mm roll: too narrow, totals still show tax


# ---------------------------------------------------------------- duplicate copy and safety

def test_first_print_changes_nothing_and_duplicate_is_marked_and_logged(shop):
    sid = _sell(shop)
    snapshot = (_count(shop.conn, "sales"), _count(shop.conn, "payments"), get_stock(shop.conn, shop.m_blue),
                shop.conn.execute("SELECT total_paise, invoice_no FROM sales").fetchone()[:])
    audit_before = _count(shop.conn, "audit_log")

    build_bill(shop.conn, sid)
    assert _count(shop.conn, "audit_log") == audit_before  # a first print writes nothing

    first = build_bill(shop.conn, sid, user_id=1, duplicate=True)
    second = build_bill(shop.conn, sid, user_id=1, duplicate=True)
    assert "Duplicate copy (copy 1)" in bill_html(first) and "Duplicate copy (copy 2)" in bill_html(second)
    logged = shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'sale_reprint'").fetchone()[0]
    assert logged == 2

    after = (_count(shop.conn, "sales"), _count(shop.conn, "payments"), get_stock(shop.conn, shop.m_blue),
             shop.conn.execute("SELECT total_paise, invoice_no FROM sales").fetchone()[:])
    assert after == snapshot  # no new sale, payment, stock change or number


def test_duplicate_needs_to_know_who_and_missing_bill_is_a_clear_error(shop):
    sid = _sell(shop)
    with pytest.raises(ValueError, match="who is reprinting"):
        build_bill(shop.conn, sid, duplicate=True)
    with pytest.raises(ValueError, match="not found"):
        build_bill(shop.conn, 9999)


def test_file_names_are_safe(shop):
    sid = _sell(shop)
    name = bill_file_name(build_bill(shop.conn, sid))
    assert name.endswith(".pdf") and "/" not in name and "\\" not in name and name.startswith("INV")
    dup = bill_file_name(build_bill(shop.conn, sid, user_id=1, duplicate=True))
    assert dup.endswith("-duplicate.pdf")