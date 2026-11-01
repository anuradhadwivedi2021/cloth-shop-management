from datetime import date

import pytest

from core.numbering import financial_year, next_number
from core.products import create_product, generate_variants
from core.stock import InsufficientStockError, add_movement, get_stock
from db.connection import open_db, transaction


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "shop.db", "test-password")
    yield c
    c.close()


def _variant(conn):
    pid = create_product(conn, "Shirt")
    return generate_variants(conn, pid, ["M"], ["Blue"])[0]


# ---------- stock movements ----------

def test_movement_must_be_inside_a_transaction(conn):
    vid = _variant(conn)
    with pytest.raises(RuntimeError):
        add_movement(conn, vid, 5, "purchase")


def test_balance_after_is_recorded(conn):
    vid = _variant(conn)
    with transaction(conn):
        assert add_movement(conn, vid, 10, "purchase") == 10
        assert add_movement(conn, vid, -3, "sale") == 7
    rows = conn.execute("SELECT balance_after FROM stock_movements ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [10, 7]
    assert get_stock(conn, vid) == 7


def test_negative_stock_is_blocked(conn):
    vid = _variant(conn)
    with transaction(conn):
        add_movement(conn, vid, 2, "purchase")
    with pytest.raises(InsufficientStockError):
        with transaction(conn):
            add_movement(conn, vid, -3, "sale")
    assert get_stock(conn, vid) == 2
    assert conn.execute("SELECT count(*) FROM stock_movements").fetchone()[0] == 1


def test_negative_stock_allowed_only_when_setting_says_so(conn):
    vid = _variant(conn)
    with transaction(conn):
        assert add_movement(conn, vid, -1, "sale", allow_negative=True) == -1


def test_buckets_are_kept_separate(conn):
    vid = _variant(conn)
    with transaction(conn):
        add_movement(conn, vid, 5, "purchase")
        add_movement(conn, vid, 2, "damaged on arrival", bucket="damaged")
    assert get_stock(conn, vid, "sellable") == 5
    assert get_stock(conn, vid, "damaged") == 2


def test_zero_quantity_and_blank_reason_are_rejected(conn):
    vid = _variant(conn)
    with transaction(conn):
        with pytest.raises(ValueError):
            add_movement(conn, vid, 0, "purchase")
        with pytest.raises(ValueError):
            add_movement(conn, vid, 1, "   ")


# ---------- numbering ----------

def test_financial_year_switches_on_1_april():
    assert financial_year(date(2026, 3, 31)) == "25-26"
    assert financial_year("2026-04-01") == "26-27"
    assert financial_year("2026-11-01") == "26-27"
    assert financial_year("2027-03-31") == "26-27"


def test_next_number_must_be_inside_a_transaction(conn):
    with pytest.raises(RuntimeError):
        next_number(conn, "INV", "26-27", "INV")


def test_numbers_are_sequential_and_formatted(conn):
    with transaction(conn):
        assert next_number(conn, "INV", "26-27", "INV") == (1, "INV/26-27/000001")
    with transaction(conn):
        assert next_number(conn, "INV", "26-27", "INV") == (2, "INV/26-27/000002")


def test_series_and_years_count_separately(conn):
    with transaction(conn):
        assert next_number(conn, "INV", "26-27", "INV")[0] == 1
        assert next_number(conn, "PUR", "26-27", "PUR")[0] == 1
        assert next_number(conn, "INV", "27-28", "INV")[0] == 1
        assert next_number(conn, "INV", "26-27", "INV")[0] == 2


def test_rolled_back_number_is_not_consumed(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            next_number(conn, "INV", "26-27", "INV")
            raise RuntimeError("crash while saving")
    with transaction(conn):
        assert next_number(conn, "INV", "26-27", "INV")[0] == 1