from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core import auth
from core.audit import verify_chain
from core.auth import AccountLocked, AuthError
from core.permissions import PermissionDenied
from db.connection import open_db

T0 = datetime(2026, 11, 1, 10, 0, 0)
PW = "correct-horse"


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", PW, pin="1234", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-pass-1", "cashier")
    yield SimpleNamespace(conn=conn, owner=owner, till=till)
    conn.close()


def _fail(conn, user, times, start=T0):
    for i in range(times):
        with pytest.raises((AuthError, AccountLocked)):
            auth.authenticate(conn, user, "wrong-password", now=start + timedelta(seconds=i))


def test_first_time_setup_only_once(tmp_path):
    conn = open_db(tmp_path / "s.db", "pw")
    assert auth.needs_setup(conn)
    auth.setup_first_owner(conn, "anu", PW)
    assert not auth.needs_setup(conn)
    with pytest.raises(ValueError, match="already done"):
        auth.setup_first_owner(conn, "other", PW)
    conn.close()


def test_passwords_and_pins_are_stored_hashed(shop):
    row = shop.conn.execute("SELECT password_hash, pin_hash FROM users WHERE id = ?", (shop.owner,)).fetchone()
    assert row["password_hash"].startswith("$argon2id$") and PW not in row["password_hash"]
    assert row["pin_hash"].startswith("$argon2id$") and "1234" not in row["pin_hash"]


@pytest.mark.parametrize("bad", ["short", "       ", "", None])
def test_weak_password_refused(tmp_path, bad):
    conn = open_db(tmp_path / "s.db", "pw")
    with pytest.raises(ValueError, match="at least 8"):
        auth.setup_first_owner(conn, "anu", bad)
    assert auth.needs_setup(conn)
    conn.close()


@pytest.mark.parametrize("pin", ["123", "1234567", "12a4", "", "١٢٣٤"])
def test_bad_pin_refused(tmp_path, pin):
    conn = open_db(tmp_path / "s.db", "pw")
    with pytest.raises(ValueError, match="4 to 6 digits"):
        auth.setup_first_owner(conn, "anu", PW, pin=pin) if pin else auth.validate_pin(pin)
    conn.close()


def test_login_ok_and_username_is_not_case_sensitive(shop):
    user = auth.authenticate(shop.conn, "ANU", PW, now=T0)
    assert user == {"id": shop.owner, "username": "anu", "role": "owner", "full_name": "Anuradha"}
    assert shop.conn.execute("SELECT last_login_at FROM users WHERE id = ?", (shop.owner,)).fetchone()[0]


def test_wrong_password_and_unknown_user_give_the_same_message(shop):
    with pytest.raises(AuthError) as a:
        auth.authenticate(shop.conn, "anu", "nope-nope", now=T0)
    with pytest.raises(AuthError) as b:
        auth.authenticate(shop.conn, "ghost", "nope-nope", now=T0)
    assert str(a.value) == str(b.value) == "Wrong username or password."


def test_lockout_after_five_wrong_passwords_and_doubling(shop):
    _fail(shop.conn, "anu", 5)
    with pytest.raises(AccountLocked) as e:
        auth.authenticate(shop.conn, "anu", PW, now=T0 + timedelta(seconds=10))  # even the right one
    assert 15 <= e.value.seconds <= 30
    # after 30 s it opens again; a 6th wrong try locks for 60 s
    later = T0 + timedelta(seconds=40)
    with pytest.raises(AuthError):
        auth.authenticate(shop.conn, "anu", "wrong-password", now=later)
    with pytest.raises(AccountLocked) as e2:
        auth.authenticate(shop.conn, "anu", PW, now=later + timedelta(seconds=1))
    assert 55 <= e2.value.seconds <= 60
    assert auth.authenticate(shop.conn, "anu", PW, now=later + timedelta(seconds=61))["id"] == shop.owner


def test_success_resets_the_counter(shop):
    _fail(shop.conn, "anu", 4)
    auth.authenticate(shop.conn, "anu", PW, now=T0 + timedelta(seconds=10))
    _fail(shop.conn, "anu", 4, start=T0 + timedelta(seconds=20))
    assert auth.authenticate(shop.conn, "anu", PW, now=T0 + timedelta(seconds=40))["id"] == shop.owner


def test_lock_is_capped_at_fifteen_minutes(shop):
    t = T0
    for _ in range(12):
        with pytest.raises((AuthError, AccountLocked)):
            auth.authenticate(shop.conn, "anu", "wrong-password", now=t)
        t += timedelta(minutes=20)
    row = shop.conn.execute("SELECT locked_until FROM users WHERE id = ?", (shop.owner,)).fetchone()
    last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    assert (last - (t - timedelta(minutes=20))).total_seconds() == 900


def test_owner_can_unlock_an_account(shop):
    _fail(shop.conn, "till", 5)
    with pytest.raises(AccountLocked):
        auth.authenticate(shop.conn, "till", "till-pass-1", now=T0 + timedelta(seconds=6))
    auth.unlock_account(shop.conn, shop.owner, shop.till)
    assert auth.authenticate(shop.conn, "till", "till-pass-1", now=T0 + timedelta(seconds=7))["id"] == shop.till
    with pytest.raises(PermissionDenied):
        auth.unlock_account(shop.conn, shop.till, shop.owner)


def test_only_owner_creates_users_and_names_are_unique(shop):
    with pytest.raises(PermissionDenied):
        auth.create_user(shop.conn, shop.till, "x1", "some-password", "cashier")
    with pytest.raises(ValueError, match="already taken"):
        auth.create_user(shop.conn, shop.owner, "TILL", "some-password", "cashier")
    with pytest.raises(ValueError, match="Role must be"):
        auth.create_user(shop.conn, shop.owner, "boss", "some-password", "king")
    with pytest.raises(ValueError, match="at least 2"):
        auth.create_user(shop.conn, shop.owner, " ", "some-password", "cashier")


def test_change_password_needs_old_one(shop):
    with pytest.raises(AuthError):
        auth.change_password(shop.conn, "till", "wrong-old-pw", "brand-new-pw", now=T0)
    auth.change_password(shop.conn, "till", "till-pass-1", "brand-new-pw", now=T0)
    with pytest.raises(AuthError):
        auth.authenticate(shop.conn, "till", "till-pass-1", now=T0)
    assert auth.authenticate(shop.conn, "till", "brand-new-pw", now=T0)["id"] == shop.till
    with pytest.raises(ValueError, match="at least 8"):
        auth.change_password(shop.conn, "till", "brand-new-pw", "short", now=T0)


def test_owner_reset_clears_lock(shop):
    _fail(shop.conn, "till", 5)
    auth.reset_password(shop.conn, shop.owner, shop.till, "fresh-password")
    assert auth.authenticate(shop.conn, "till", "fresh-password", now=T0 + timedelta(seconds=6))["id"] == shop.till
    with pytest.raises(PermissionDenied):
        auth.reset_password(shop.conn, shop.till, shop.owner, "hacked-password")


def test_deactivated_user_cannot_sign_in_and_history_stays(shop):
    auth.set_user_active(shop.conn, shop.owner, shop.till, False)
    with pytest.raises(AuthError):
        auth.authenticate(shop.conn, "till", "till-pass-1", now=T0)
    assert shop.conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (shop.till,)).fetchone()[0] == 1
    auth.set_user_active(shop.conn, shop.owner, shop.till, True)
    assert auth.authenticate(shop.conn, "till", "till-pass-1", now=T0)["id"] == shop.till


def test_cannot_deactivate_self_or_last_owner(shop):
    with pytest.raises(ValueError, match="your own"):
        auth.set_user_active(shop.conn, shop.owner, shop.owner, False)
    second = auth.create_user(shop.conn, shop.owner, "anu2", "second-owner", "owner")
    auth.set_user_active(shop.conn, second, shop.owner, False)  # fine: another owner remains
    with pytest.raises(ValueError, match="last active owner|your own"):
        auth.set_user_active(shop.conn, second, second, False)


def test_owner_pin_approval_and_throttle(shop):
    assert auth.verify_owner_pin(shop.conn, "1234", now=T0) == shop.owner
    for i in range(5):
        with pytest.raises(AuthError, match="Wrong PIN"):
            auth.verify_owner_pin(shop.conn, "0000", now=T0 + timedelta(seconds=i))
    with pytest.raises(AccountLocked):
        auth.verify_owner_pin(shop.conn, "1234", now=T0 + timedelta(seconds=6))
    assert auth.verify_owner_pin(shop.conn, "1234", now=T0 + timedelta(seconds=60)) == shop.owner


def test_no_pin_set_and_set_pin_flow(tmp_path):
    conn = open_db(tmp_path / "s.db", "pw")
    auth.setup_first_owner(conn, "anu", PW)
    with pytest.raises(AuthError, match="No owner PIN"):
        auth.verify_owner_pin(conn, "1234")
    with pytest.raises(AuthError):
        auth.set_pin(conn, "anu", "wrong-password", "4321", now=T0)
    auth.set_pin(conn, "anu", PW, "4321", now=T0)
    assert auth.verify_owner_pin(conn, "4321", now=T0) == 1
    conn.close()


def test_cashier_pin_is_not_an_owner_pin(shop):
    auth.set_pin(shop.conn, "till", "till-pass-1", "9999", now=T0)
    with pytest.raises(AuthError, match="Wrong PIN"):
        auth.verify_owner_pin(shop.conn, "9999", now=T0)


def test_audit_log_records_logins_and_never_secrets(shop):
    auth.authenticate(shop.conn, "anu", PW, now=T0)
    _fail(shop.conn, "ghost", 1)
    auth.verify_owner_pin(shop.conn, "1234", now=T0)
    text = " ".join(
        f"{r['action']} {r['before_json']} {r['after_json']}" for r in shop.conn.execute("SELECT * FROM audit_log")
    )
    actions = {r["action"] for r in shop.conn.execute("SELECT action FROM audit_log")}
    assert {"owner_created", "user_created", "login", "login_failed", "owner_pin_approved"} <= actions
    for secret in (PW, "till-pass-1", "wrong-password", "1234"):
        assert secret not in text
    assert verify_chain(shop.conn) == (True, None)
