from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core import auth, user_admin
from core.audit import verify_chain
from core.permissions import PermissionDenied, set_user_permission
from db.connection import open_db

PW = "correct-horse"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", PW, pin="1234", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-pass-1", "cashier")
    boss2 = auth.create_user(conn, owner, "boss2", "boss2-pass-1", "owner")
    yield SimpleNamespace(conn=conn, owner=owner, till=till, boss2=boss2)
    conn.close()


def _audit_actions(conn):
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


def test_list_users_shows_status_and_never_secrets(shop):
    users = user_admin.list_users(shop.conn, shop.owner)
    assert [u["username"] for u in users] == ["anu", "till", "boss2"]
    assert users[0]["has_pin"] and not users[1]["has_pin"]
    assert users[1]["role_label"] == "Cashier" and users[1]["status"] == "Active"
    for u in users:
        assert "password_hash" not in u and "pin_hash" not in u


def test_list_users_needs_manage_users(shop):
    with pytest.raises(PermissionDenied):
        user_admin.list_users(shop.conn, shop.till)


def test_locked_and_disabled_status(shop):
    now = datetime(2026, 11, 3, 10, 0, 0)
    shop.conn.execute("UPDATE users SET locked_until = ? WHERE id = ?", ("2026-11-03 10:05:00", shop.till))
    assert user_admin.list_users(shop.conn, shop.owner, now=now)[1]["status"] == "Locked"
    assert user_admin.list_users(shop.conn, shop.owner, now=now + timedelta(minutes=10))[1]["status"] == "Active"
    user_admin.set_active(shop.conn, shop.owner, shop.till, False)
    assert user_admin.list_users(shop.conn, shop.owner, now=now)[1]["status"] == "Disabled"


def test_add_user_stores_a_hash_not_the_password(shop):
    uid = user_admin.add_user(shop.conn, shop.owner, "ravi", "ravi-pass-99", "Stock staff", pin="4321", full_name="Ravi")
    row = shop.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["role"] == "stock"
    assert "ravi-pass-99" not in row["password_hash"] and "4321" not in (row["pin_hash"] or "")
    assert auth.authenticate(shop.conn, "ravi", "ravi-pass-99")["id"] == uid
    assert "user_created" in _audit_actions(shop.conn)


def test_existing_username_is_refused_and_owner_row_is_untouched(shop):
    before = shop.conn.execute("SELECT password_hash, role, pin_hash FROM users WHERE id = ?", (shop.owner,)).fetchone()
    with pytest.raises(ValueError, match="already taken"):
        user_admin.add_user(shop.conn, shop.owner, "ANU", "hacker-pass-1", "Cashier")
    after = shop.conn.execute("SELECT password_hash, role, pin_hash FROM users WHERE id = ?", (shop.owner,)).fetchone()
    assert tuple(before) == tuple(after)
    assert auth.authenticate(shop.conn, "anu", PW)["id"] == shop.owner


@pytest.mark.parametrize("bad_password", ["", "short", "       "])
def test_weak_password_refused_and_nothing_saved(shop, bad_password):
    with pytest.raises(ValueError):
        user_admin.add_user(shop.conn, shop.owner, "newbie", bad_password, "Cashier")
    assert shop.conn.execute("SELECT COUNT(*) FROM users WHERE username = 'newbie'").fetchone()[0] == 0


def test_bad_pin_refused_and_nothing_saved(shop):
    with pytest.raises(ValueError, match="4 to 6 digits"):
        user_admin.add_user(shop.conn, shop.owner, "newbie", "newbie-pass-1", "Cashier", pin="12")
    assert shop.conn.execute("SELECT COUNT(*) FROM users WHERE username = 'newbie'").fetchone()[0] == 0


def test_unknown_role_refused(shop):
    with pytest.raises(ValueError, match="valid role"):
        user_admin.add_user(shop.conn, shop.owner, "x1", "x1-password-1", "Manager")


def test_cashier_cannot_create_users(shop):
    with pytest.raises(PermissionDenied):
        user_admin.add_user(shop.conn, shop.till, "sneaky", "sneaky-pass-1", "Cashier")


def test_helper_with_manage_users_cannot_create_an_owner(shop):
    set_user_permission(shop.conn, shop.owner, shop.till, "manage_users", True)
    uid = user_admin.add_user(shop.conn, shop.till, "helper2", "helper2-pass-1", "Cashier")
    assert uid
    with pytest.raises(PermissionDenied, match="Only an owner"):
        user_admin.add_user(shop.conn, shop.till, "evil", "evil-pass-123", "Owner")


def test_helper_cannot_take_over_an_owner_account(shop):
    set_user_permission(shop.conn, shop.owner, shop.till, "manage_users", True)
    for call in (
        lambda: user_admin.reset_password(shop.conn, shop.till, shop.owner, "stolen-pass-1"),
        lambda: user_admin.set_active(shop.conn, shop.till, shop.boss2, False),
        lambda: user_admin.unlock(shop.conn, shop.till, shop.owner),
    ):
        with pytest.raises(PermissionDenied, match="Only an owner"):
            call()
    assert auth.authenticate(shop.conn, "anu", PW)["id"] == shop.owner


def test_owner_resets_a_password_and_old_one_stops_working(shop):
    user_admin.reset_password(shop.conn, shop.owner, shop.till, "brand-new-pass")
    assert auth.authenticate(shop.conn, "till", "brand-new-pass")["id"] == shop.till
    with pytest.raises(auth.AuthError):
        auth.authenticate(shop.conn, "till", "till-pass-1")
    assert "password_reset" in _audit_actions(shop.conn)


def test_reset_with_weak_password_changes_nothing(shop):
    with pytest.raises(ValueError):
        user_admin.reset_password(shop.conn, shop.owner, shop.till, "short")
    assert auth.authenticate(shop.conn, "till", "till-pass-1")["id"] == shop.till


def test_reset_unknown_user(shop):
    with pytest.raises(ValueError, match="not found"):
        user_admin.reset_password(shop.conn, shop.owner, 9999, "whatever-pass-1")


def test_disable_and_enable_keeps_history(shop):
    user_admin.set_active(shop.conn, shop.owner, shop.till, False)
    with pytest.raises(auth.AuthError):
        auth.authenticate(shop.conn, "till", "till-pass-1")
    user_admin.set_active(shop.conn, shop.owner, shop.till, True)
    assert auth.authenticate(shop.conn, "till", "till-pass-1")["id"] == shop.till
    assert shop.conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (shop.till,)).fetchone()[0] == 1


def test_cannot_disable_yourself_or_the_last_owner(shop):
    with pytest.raises(ValueError, match="own account"):
        user_admin.set_active(shop.conn, shop.owner, shop.owner, False)
    user_admin.set_active(shop.conn, shop.owner, shop.boss2, False)
    with pytest.raises(ValueError):
        user_admin.set_active(shop.conn, shop.boss2, shop.owner, False)


def test_unlock_clears_a_lock(shop):
    shop.conn.execute("UPDATE users SET failed_attempts = 5, locked_until = '2099-01-01 00:00:00' WHERE id = ?", (shop.till,))
    user_admin.unlock(shop.conn, shop.owner, shop.till)
    assert auth.authenticate(shop.conn, "till", "till-pass-1")["id"] == shop.till
    assert "account_unlocked" in _audit_actions(shop.conn)


def test_audit_chain_is_still_valid_after_user_admin(shop):
    user_admin.add_user(shop.conn, shop.owner, "ravi", "ravi-pass-99", "Cashier")
    user_admin.reset_password(shop.conn, shop.owner, shop.till, "brand-new-pass")
    user_admin.set_active(shop.conn, shop.owner, shop.till, False)
    assert verify_chain(shop.conn) == (True, None)
