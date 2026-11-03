"""SRS v2.0 worked bill (section 6), checked to the paisa through the REAL code paths.

The expected figures below are typed in from the SRS table. Nothing here is computed
by the test itself, so a wrong tax engine or a wrong saved bill cannot pass.

  Shirt, M, blue    qty 2   tag Rs 1,050   5%    taxable Rs 2,000.00   tax Rs 100.00
  Blazer, L, black  qty 1   tag Rs 4,130   18%   taxable Rs 3,500.00   tax Rs 630.00
  Bill total Rs 6,230       taxable Rs 5,500.00   tax Rs 730.00
"""

from types import SimpleNamespace

import pytest

from core.products import create_product, generate_variants
from core.sales import create_sale, get_sale
from core.stock import add_movement
from core.tax import DEFAULT_TAX_RULES, calculate_bill, ensure_default_tax_rules
from db.connection import open_db, transaction

DAY = "2026-11-01"

SRS_LINES = [
    # rate %, taxable paise, tax paise, line total paise
    (5.0, 200000, 10000, 210000),
    (18.0, 350000, 63000, 413000),
]
SRS_TOTALS = {"taxable_paise": 550000, "tax_paise": 73000, "round_off_paise": 0, "total_paise": 623000}


def _rules():
    return [
        {"id": i, "hsn_prefix": p, "rate_percent": r, "price_ceiling_paise": c,
         "valid_from": vf, "valid_to": None, "note": n}
        for i, (p, r, c, vf, n) in enumerate(DEFAULT_TAX_RULES, start=1)
    ]


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    blazer = create_product(conn, "Blazer", hsn="6202", default_price_paise=413000)
    (m_blue,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    (l_black,) = generate_variants(conn, blazer, ["L"], ["Black"])
    with transaction(conn):
        add_movement(conn, m_blue, 10, "opening stock")
        add_movement(conn, l_black, 10, "opening stock")
    yield SimpleNamespace(conn=conn, m_blue=m_blue, l_blue=None, l_black=l_black)
    conn.close()


def _check_lines(lines):
    assert len(lines) == len(SRS_LINES)
    for got, (rate, taxable, tax, total) in zip(lines, SRS_LINES):
        assert got["taxable_paise"] == taxable
        assert got["tax_paise"] == tax
        assert taxable + tax == total


def test_tax_engine_reproduces_srs_worked_bill():
    bill = calculate_bill(
        [
            {"qty": 2, "unit_price_paise": 105000, "hsn": "6205"},
            {"qty": 1, "unit_price_paise": 413000, "hsn": "6202"},
        ],
        on_date=DAY, rules=_rules(), mode="regular", prices_include_tax=True,
    )
    assert [line["rate_percent"] for line in bill["lines"]] == [5.0, 18.0]
    _check_lines(bill["lines"])
    for key, expected in SRS_TOTALS.items():
        assert bill[key] == expected, key


def test_saved_bill_reproduces_srs_worked_bill(shop):
    sale_id = create_sale(
        shop.conn,
        [{"variant_id": shop.m_blue, "qty": 2}, {"variant_id": shop.l_black, "qty": 1}],
        [{"mode": "cash", "amount_paise": 623000}],
        user_id=1, on_date=DAY,
    )
    data = get_sale(shop.conn, sale_id)
    assert [line["tax_rate_percent"] for line in data["lines"]] == [5.0, 18.0]
    _check_lines(data["lines"])
    for key, expected in SRS_TOTALS.items():
        assert data["sale"][key] == expected, key
    assert data["is_fully_paid"]
