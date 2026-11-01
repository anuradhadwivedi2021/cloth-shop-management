from types import SimpleNamespace

import pytest

from core import auth
from core.audit import verify_chain
from core.auth import AccountLocked, AuthError
from core.guarded import return_items, sell
from core.permissions import PermissionDenied, set_discount_cap, set_user_permission
from core.products import create_product, generate_variants
from core.returns import returnable_lines
from core.settings import set_setting
from core.stock import add_movement, get_stock
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction

CASH = [{"mode": "cash", "amount_paise": 10 ** 7}]


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", pin="1234")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    store = auth.create_user(conn, owner, "store", "store-password", "stock")
    ensure_default_tax_rules(conn)
    pid = create_product(conn, "Shirt", hsn="6205", default_price_paise=100000, category="Shirts")  # Rs 1,000
    (m_blue,) = generate_variants(conn, pid, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, m_blue, 20, "opening stock")
    yield SimpleNamespace(conn=conn, owner=owner, till=till, store=store, v=m_blue)
    conn.close()


def _cart(s, qty=1, **extra):
    return [{"variant_id": s.v, "qty": qty, **extra}]


def test_cashier_sells_without_discount(shop):
    sid = sell(shop.conn, _cart(shop), CASH, user_id=shop.till, on_date="2026-11-01")
    assert shop.conn.execute("SELECT user_id FROM sales WHERE id = ?", (sid,)).fetchone()[0] == shop.till
    assert get_stock(shop.conn, shop.v) == 19


def test_user_without_sell_permission_cannot_sell(shop):
    with pytest.raises(PermissionDenied, match="make a sale"):
        sell(shop.conn, _cart(shop), CASH, user_id=shop.store)
    set_user_permission(shop.conn, shop.owner, shop.till, "sell", False)
    with pytest.raises(PermissionDenied):
        sell(shop.conn, _cart(shop), CASH, user_id=shop.till)
    assert get_stock(shop.conn, shop.v) == 20 and shop.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_discount_within_limit_needs_no_pin(shop):
    sell(shop.conn, _cart(shop, 2, discount_paise=20000), CASH, user_id=shop.till, on_date="2026-11-01")  # 10% of 2,000
    assert get_stock(shop.conn, shop.v) == 18


def test_discount_above_limit_needs_owner_pin(shop):
    with pytest.raises(PermissionDenied, match="owner's PIN"):
        sell(shop.conn, _cart(shop, discount_paise=20000), CASH, user_id=shop.till)  # 20%
    with pytest.raises(PermissionDenied, match="owner's PIN"):
        sell(shop.conn, _cart(shop), CASH, user_id=shop.till, bill_discount_paise=15000)  # bill-level 15%
    assert get_stock(shop.conn, shop.v) == 20


def test_wrong_pin_blocks_the_sale_and_right_pin_is_logged(shop):
    with pytest.raises(AuthError, match="Wrong PIN"):
        sell(shop.conn, _cart(shop, discount_paise=20000), CASH, user_id=shop.till, approver_pin="0000")
    assert get_stock(shop.conn, shop.v) == 20
    sid = sell(shop.conn, _cart(shop, discount_paise=20000), CASH, user_id=shop.till,
               approver_pin="1234", on_date="2026-11-01")
    assert get_stock(shop.conn, shop.v) == 19
    ev = shop.conn.execute("SELECT * FROM audit_log WHERE action = 'discount_override'").fetchone()
    assert ev["entity_id"] == sid and ev["user_id"] == shop.till and f'"approved_by": {shop.owner}' in ev["after_json"]
    assert verify_chain(shop.conn) == (True, None)


def test_wrong_pin_failures_are_remembered_even_though_the_sale_failed(shop):
    for _ in range(5):
        with pytest.raises(AuthError):
            sell(shop.conn, _cart(shop, discount_paise=20000), CASH, user_id=shop.till, approver_pin="0000")
    with pytest.raises(AccountLocked):
        sell(shop.conn, _cart(shop, discount_paise=20000), CASH, user_id=shop.till, approver_pin="1234")


def test_owner_discount_and_custom_limit(shop):
    sell(shop.conn, _cart(shop, discount_paise=50000), CASH, user_id=shop.owner, on_date="2026-11-01")  # owner: up to 100%
    set_discount_cap(shop.conn, shop.owner, shop.till, 25)
    sell(shop.conn, _cart(shop, discount_paise=20000), CASH, user_id=shop.till, on_date="2026-11-01")  # 20% <= 25%
    assert get_stock(shop.conn, shop.v) == 18


def test_stock_staff_cannot_discount_even_with_pin_path(shop):
    set_user_permission(shop.conn, shop.owner, shop.store, "sell", True)
    with pytest.raises(PermissionDenied, match="give a discount"):
        sell(shop.conn, _cart(shop, discount_paise=1000), CASH, user_id=shop.store, approver_pin="1234")


def test_retry_with_same_key_does_not_sell_twice_or_ask_for_pin_again(shop):
    args = dict(user_id=shop.till, idempotency_key="bill-1", approver_pin="1234", on_date="2026-11-01")
    a = sell(shop.conn, _cart(shop, discount_paise=20000), CASH, **args)
    b = sell(shop.conn, _cart(shop, discount_paise=20000), CASH, **args)
    assert a == b and get_stock(shop.conn, shop.v) == 19
    assert shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'discount_override'").fetchone()[0] == 1


def test_bad_cart_is_reported_by_the_sale_itself(shop):
    with pytest.raises(ValueError, match="at least one item"):
        sell(shop.conn, [], CASH, user_id=shop.till)
    with pytest.raises(ValueError, match="not found"):
        sell(shop.conn, [{"variant_id": 999, "qty": 1}], CASH, user_id=shop.till)


# ---------- returns ----------

def _sold(shop):
    sid = sell(shop.conn, _cart(shop, 2), CASH, user_id=shop.till, on_date="2026-11-01")
    return sid, returnable_lines(shop.conn, sid)[0]["sale_line_id"]


def test_cashier_returns_within_policy(shop):
    sid, line = _sold(shop)
    rid = return_items(shop.conn, sid, [{"sale_line_id": line, "qty": 1}], user_id=shop.till,
                       reason="wrong_size", on_date="2026-11-02")
    assert rid and get_stock(shop.conn, shop.v, "returned_pending") == 1


def test_stock_staff_cannot_process_customer_returns(shop):
    sid, line = _sold(shop)
    with pytest.raises(PermissionDenied, match="process returns"):
        return_items(shop.conn, sid, [{"sale_line_id": line, "qty": 1}], user_id=shop.store, reason="x")


def test_policy_override_needs_owner_pin_and_reason(shop):
    set_setting(shop.conn, "return_window_days", 7)
    sid, line = _sold(shop)
    late = dict(reason="defect", on_date="2026-12-31")
    with pytest.raises(ValueError, match="Only the owner can override"):
        return_items(shop.conn, sid, [{"sale_line_id": line, "qty": 1}], user_id=shop.till, **late)
    with pytest.raises(AuthError, match="Wrong PIN"):
        return_items(shop.conn, sid, [{"sale_line_id": line, "qty": 1}], user_id=shop.till,
                     approver_pin="0000", override_reason="regular customer", **late)
    with pytest.raises(ValueError, match="reason"):
        return_items(shop.conn, sid, [{"sale_line_id": line, "qty": 1}], user_id=shop.till,
                     approver_pin="1234", **late)
    rid = return_items(shop.conn, sid, [{"sale_line_id": line, "qty": 1}], user_id=shop.till,
                       approver_pin="1234", override_reason="regular customer", **late)
    row = shop.conn.execute("SELECT override_user_id, override_reason FROM sale_returns WHERE id = ?", (rid,)).fetchone()
    assert (row[0], row[1]) == (shop.owner, "regular customer")


def test_signed_in_owner_can_override_with_a_reason(shop):
    set_setting(shop.conn, "return_window_days", 7)
    sid, line = _sold(shop)
    rid = return_items(shop.conn, sid, [{"sale_line_id": line, "qty": 1}], user_id=shop.owner,
                       reason="defect", override_reason="owner decision", on_date="2026-12-31")
    assert rid
