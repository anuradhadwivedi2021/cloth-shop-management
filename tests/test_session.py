import pytest

from core import auth
from core.auth import AuthError
from core.permissions import PermissionDenied
from core.session import Session, SessionLocked, auto_lock_minutes
from core.settings import set_setting
from db.connection import open_db


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    conn = open_db(tmp_path / "s.db", "pw")
    owner = auth.setup_first_owner(conn, "anu", "correct-horse")
    auth.create_user(conn, owner, "till", "till-pass-1", "cashier")
    clock = Clock()
    user = auth.authenticate(conn, "till", "till-pass-1")
    yield conn, Session(user, timeout_minutes=5, clock=clock), clock
    conn.close()


def test_session_locks_after_idle_time(env):
    conn, s, clock = env
    clock.t += 299
    s.touch()
    clock.t += 299       # activity restarted the timer
    s.touch()
    clock.t += 300
    assert s.locked
    with pytest.raises(SessionLocked):
        s.touch()
    with pytest.raises(SessionLocked):
        s.require(conn, "sell")


def test_manual_lock_and_unlock_needs_the_same_password(env):
    conn, s, clock = env
    s.lock()
    assert s.locked
    with pytest.raises(AuthError):
        s.unlock(conn, "wrong-password")
    assert s.locked
    s.unlock(conn, "till-pass-1")
    assert not s.locked
    s.touch()


def test_unlock_after_timeout(env):
    conn, s, clock = env
    clock.t += 400
    assert s.locked
    s.unlock(conn, "till-pass-1")
    s.require(conn, "sell")


def test_other_users_password_cannot_unlock(env):
    conn, s, _ = env
    s.lock()
    with pytest.raises(AuthError):
        s.unlock(conn, "correct-horse")  # the owner's password, not this cashier's


def test_require_checks_permission_live(env):
    conn, s, _ = env
    assert s.require(conn, "sell")["role"] == "cashier"
    with pytest.raises(PermissionDenied):
        s.require(conn, "view_cost")
    conn.execute("UPDATE users SET is_active = 0 WHERE username = 'till'")  # deactivated mid-session
    with pytest.raises(PermissionDenied):
        s.require(conn, "sell")


def test_timeout_setting(env):
    conn, _, _ = env
    assert auto_lock_minutes(conn) == 5
    set_setting(conn, "auto_lock_minutes", 2)
    assert auto_lock_minutes(conn) == 2
    set_setting(conn, "auto_lock_minutes", "abc")
    assert auto_lock_minutes(conn) == 5
    with pytest.raises(ValueError, match="at least 1"):
        Session({"id": 1, "username": "x", "role": "owner"}, timeout_minutes=0)


def test_timeout_can_be_changed_while_signed_in(env):
    conn, s, clock = env
    assert s.timeout_minutes == 5
    s.set_timeout_minutes(15)
    assert s.timeout_minutes == 15
    clock.t += 600       # more than the old 5 minutes, less than the new 15
    assert not s.locked
    clock.t += 400
    assert s.locked


@pytest.mark.parametrize("bad", [0, -3, 2.5, "10", None, True])
def test_bad_timeout_is_refused_and_old_value_kept(env, bad):
    conn, s, clock = env
    with pytest.raises(ValueError):
        s.set_timeout_minutes(bad)
    assert s.timeout_minutes == 5


def test_changing_timeout_does_not_unlock_a_locked_session(env):
    conn, s, clock = env
    s.lock()
    s.set_timeout_minutes(30)
    assert s.locked


def test_first_time_setup_sequence_used_by_the_wizard(tmp_path):
    """The same calls the setup wizard makes after creating the shop (this is what crashed the real app)."""
    from core import shop
    from core.shop_settings import update_preferences

    conn, session, _recovery = shop.setup_new_shop(tmp_path, "owner", "Demo@12345", pin="1234", full_name="Anu")
    update_preferences(conn, session.user_id, {"auto_lock_minutes": 15})
    session.set_timeout_minutes(15)
    assert session.timeout_minutes == 15 and auto_lock_minutes(conn) == 15
    conn.close()

