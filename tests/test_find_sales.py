"""RET-01: find the original bill by number, customer phone or date (for returns and exchanges)."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import shop
from core.customers import create_customer
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.sales import create_sale, find_sales
from core.stock import add_movement
from core.tax import ensure_default_tax_rules
from db.connection import transaction

TODAY = date.today()
CASH = [{"mode": "cash", "amount_paise": 105000}]


@pytest.fixture
def s(tmp_path):
    folder = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    shop.add_user(folder, conn, owner, "store", "store-password", "stock")
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 50, "opening stock")
    uid = owner.user_id
    anu = create_customer(conn, "9876543210", user_id=uid, name="Anu", consent_given=True, consent_purpose="Billing")
    ravi = create_customer(conn, "9123456789", user_id=uid, name="Ravi", consent_given=True, consent_purpose="Billing")

    def sell(customer=None):
        sale_id = create_sale(conn, [{"variant_id": variant, "qty": 1}], CASH, user_id=uid, customer_id=customer)
        return sale_id, conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (sale_id,)).fetchone()["invoice_no"]

    anu_bill, anu_no = sell(anu)
    ravi_bill, ravi_no = sell(ravi)
    walk_bill, walk_no = sell(None)
    users = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users")}
    yield SimpleNamespace(conn=conn, uid=uid, users=users, anu=anu_bill, anu_no=anu_no, ravi=ravi_bill,
                          walk=walk_bill, walk_no=walk_no)
    conn.close()


def ids(rows):
    return {r["id"] for r in rows}


def test_find_by_whole_or_part_of_the_bill_number_ignoring_case(s):
    assert ids(find_sales(s.conn, user_id=s.uid, text=s.anu_no)) == {s.anu}
    assert ids(find_sales(s.conn, user_id=s.uid, text=s.anu_no.lower())) == {s.anu}
    last_digits = s.anu_no[-3:]
    assert s.anu in ids(find_sales(s.conn, user_id=s.uid, text=last_digits))


def test_percent_and_underscore_are_ordinary_characters(s):
    assert find_sales(s.conn, user_id=s.uid, text="%") == []
    assert find_sales(s.conn, user_id=s.uid, text="_") == []


def test_find_by_phone_in_any_common_way_of_writing_it(s):
    for typed in ("9876543210", "98765", "3210", "+91 98765 43210", "098765-43210"):
        assert ids(find_sales(s.conn, user_id=s.uid, phone=typed)) == {s.anu}, typed
    assert ids(find_sales(s.conn, user_id=s.uid, phone="91234")) == {s.ravi}


def test_a_walk_in_bill_is_never_found_by_phone(s):
    assert s.walk not in ids(find_sales(s.conn, user_id=s.uid, phone="0000"))
    assert s.walk not in ids(find_sales(s.conn, user_id=s.uid, phone="98765"))


def test_a_phone_that_is_too_short_is_refused(s):
    with pytest.raises(ValueError, match="at least 4 digits"):
        find_sales(s.conn, user_id=s.uid, phone="98")


def test_find_by_date_range(s):
    yesterday, tomorrow = (TODAY - timedelta(days=1)).isoformat(), (TODAY + timedelta(days=1)).isoformat()
    both = find_sales(s.conn, user_id=s.uid, date_from=TODAY.isoformat(), date_to=TODAY.isoformat())
    assert ids(both) == {s.anu, s.ravi, s.walk}
    assert len(find_sales(s.conn, user_id=s.uid, date_from=yesterday)) == 3
    assert find_sales(s.conn, user_id=s.uid, date_to=yesterday) == []
    assert find_sales(s.conn, user_id=s.uid, date_from=tomorrow) == []
    assert find_sales(s.conn, user_id=s.uid, date_from="2000-01-01", date_to="2000-01-31") == []


def test_bad_dates_are_refused_with_a_clear_message(s):
    with pytest.raises(ValueError, match="first date must be a valid date"):
        find_sales(s.conn, user_id=s.uid, date_from="31/02/2026")
    with pytest.raises(ValueError, match="second date must be a valid date"):
        find_sales(s.conn, user_id=s.uid, date_to="nonsense")
    with pytest.raises(ValueError, match="must not be after"):
        find_sales(s.conn, user_id=s.uid, date_from="2026-11-05", date_to="2026-11-01")


def test_search_terms_work_together(s):
    day = TODAY.isoformat()
    assert ids(find_sales(s.conn, user_id=s.uid, phone="98765", date_from=day, date_to=day)) == {s.anu}
    assert find_sales(s.conn, user_id=s.uid, phone="98765", date_to="2000-01-01") == []
    assert find_sales(s.conn, user_id=s.uid, text=s.walk_no, phone="98765") == []


def test_at_least_one_search_term_is_needed(s):
    for blank in ({}, {"text": "  ", "phone": "", "date_from": None}):
        with pytest.raises(ValueError, match="Enter a bill number"):
            find_sales(s.conn, user_id=s.uid, **blank)


def test_newest_first_and_the_limit_is_kept(s):
    rows = find_sales(s.conn, user_id=s.uid, date_from=TODAY.isoformat())
    assert [r["id"] for r in rows] == sorted(ids(rows), reverse=True)
    assert len(find_sales(s.conn, user_id=s.uid, date_from=TODAY.isoformat(), limit=2)) == 2
    assert len(find_sales(s.conn, user_id=s.uid, date_from=TODAY.isoformat(), limit=0)) == 1  # at least one


def test_no_customer_details_come_back(s):
    (row,) = find_sales(s.conn, user_id=s.uid, text=s.anu_no)
    assert set(row) == {"id", "invoice_no", "created_at", "total_paise", "status", "has_customer"}
    assert row["has_customer"] is True and row["status"] == "completed" and row["total_paise"] == 105000
    (walk,) = find_sales(s.conn, user_id=s.uid, text=s.walk_no)
    assert walk["has_customer"] is False


def test_who_may_search(s):
    assert find_sales(s.conn, user_id=s.users["till"], text=s.anu_no)  # a cashier handles returns
    with pytest.raises(PermissionDenied, match="not allowed to find bills"):
        find_sales(s.conn, user_id=s.users["store"], text=s.anu_no)


def test_searching_changes_nothing(s):
    before = [tuple(r) for r in s.conn.execute("SELECT * FROM sales ORDER BY id")]
    audit_before = s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    find_sales(s.conn, user_id=s.uid, phone="98765")
    assert [tuple(r) for r in s.conn.execute("SELECT * FROM sales ORDER BY id")] == before
    assert s.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audit_before
