from types import SimpleNamespace

import pytest

from core.audit import verify_chain
from core.permissions import (
    ALL_PERMISSIONS, ROLE_DEFAULTS, PermissionDenied, clear_user_permission, discount_cap_percent,
    has_permission, require_permission, set_discount_cap, set_user_permission, user_permissions,
)
from db.connection import open_db


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    for name, role in [("owner", "owner"), ("till", "cashier"), ("store", "stock"), ("ca", "accountant")]:
        conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    yield SimpleNamespace(conn=conn, owner=1, cashier=2, stock=3, accountant=4)
    conn.close()


def test_role_defaults_match_the_srs(shop):
    c = shop.conn
    assert has_permission(c, shop.cashier, "sell") and has_permission(c, shop.cashier, "process_return")
    assert not has_permission(c, shop.cashier, "view_cost")        # cashier: no cost price
    assert not has_permission(c, shop.cashier, "adjust_stock")     # cashier: no stock edit
    assert not has_permission(c, shop.cashier, "restore_backup")
    assert has_permission(c, shop.stock, "receive_purchase") and not has_permission(c, shop.stock, "sell")
    assert not has_permission(c, shop.stock, "view_reports")       # storekeeper: no sales reports
    assert has_permission(c, shop.accountant, "view_reports") and has_permission(c, shop.accountant, "view_cost")
    assert not has_permission(c, shop.accountant, "sell")          # accountant is read-only
    assert not has_permission(c, shop.accountant, "adjust_stock")
    assert user_permissions(c, shop.owner) == set(ALL_PERMISSIONS)


def test_every_default_permission_is_a_known_one():
    for perms in ROLE_DEFAULTS.values():
        assert perms <= set(ALL_PERMISSIONS)


def test_require_permission_raises_a_clear_error(shop):
    with pytest.raises(PermissionDenied, match="not allowed to view costs"):
        require_permission(shop.conn, shop.cashier, "view_cost", "view costs")
    with pytest.raises(ValueError):  # PermissionDenied is a ValueError
        require_permission(shop.conn, shop.cashier, "manage_users")
    with pytest.raises(ValueError, match="Unknown permission"):
        require_permission(shop.conn, shop.owner, "fly")
    assert require_permission(shop.conn, shop.owner, "manage_users")["role"] == "owner"


def test_inactive_or_unknown_user_has_nothing(shop):
    shop.conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (shop.cashier,))
    assert not has_permission(shop.conn, shop.cashier, "sell")
    with pytest.raises(PermissionDenied, match="not found or not active"):
        require_permission(shop.conn, shop.cashier, "sell")
    assert not has_permission(shop.conn, 999, "sell")


def test_owner_can_grant_and_remove_single_permissions(shop):
    c = shop.conn
    set_user_permission(c, shop.owner, shop.cashier, "view_cost", True)
    set_user_permission(c, shop.owner, shop.cashier, "process_return", False)
    assert has_permission(c, shop.cashier, "view_cost")
    assert not has_permission(c, shop.cashier, "process_return")
    assert has_permission(c, shop.cashier, "sell")  # untouched
    clear_user_permission(c, shop.owner, shop.cashier, "process_return")
    assert has_permission(c, shop.cashier, "process_return")  # role default again
    assert verify_chain(c) == (True, None)


def test_only_manage_users_can_change_permissions(shop):
    with pytest.raises(PermissionDenied):
        set_user_permission(shop.conn, shop.cashier, shop.cashier, "view_cost", True)
    with pytest.raises(PermissionDenied):
        set_user_permission(shop.conn, shop.accountant, shop.stock, "sell", True)
    assert not has_permission(shop.conn, shop.cashier, "view_cost")


def test_even_a_granted_manage_users_is_owner_controlled(shop):
    set_user_permission(shop.conn, shop.owner, shop.stock, "manage_users", True)
    set_user_permission(shop.conn, shop.stock, shop.cashier, "view_cost", True)  # now allowed
    assert has_permission(shop.conn, shop.cashier, "view_cost")


def test_owner_cannot_be_restricted_and_bad_input_is_refused(shop):
    with pytest.raises(ValueError, match="cannot be restricted"):
        set_user_permission(shop.conn, shop.owner, shop.owner, "sell", False)
    with pytest.raises(ValueError, match="Unknown permission"):
        set_user_permission(shop.conn, shop.owner, shop.cashier, "fly", True)
    with pytest.raises(ValueError, match="User not found"):
        set_user_permission(shop.conn, shop.owner, 999, "sell", True)
    assert user_permissions(shop.conn, shop.owner) == set(ALL_PERMISSIONS)


def test_discount_caps(shop):
    c = shop.conn
    assert discount_cap_percent(c, shop.owner) == 100
    assert discount_cap_percent(c, shop.cashier) == 10
    assert discount_cap_percent(c, shop.accountant) == 0
    set_discount_cap(c, shop.owner, shop.cashier, 25)
    assert discount_cap_percent(c, shop.cashier) == 25
    set_discount_cap(c, shop.owner, shop.cashier, None)
    assert discount_cap_percent(c, shop.cashier) == 10
    with pytest.raises(ValueError, match="between 0 and 100"):
        set_discount_cap(c, shop.owner, shop.cashier, 101)
    with pytest.raises(PermissionDenied):
        set_discount_cap(c, shop.cashier, shop.cashier, 90)
