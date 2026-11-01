import pytest

from core import auth, keystore, shop
from core.auth import AccountLocked, AuthError
from core.keystore import KeystoreError
from core.permissions import PermissionDenied
from core.session import SessionLocked
from db.connection import WrongPasswordError, open_db


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "myshop"


def test_setup_creates_encrypted_shop_and_recovery_key(folder):
    conn, session, recovery = shop.setup_new_shop(folder, "anu", "owner-password", pin="1234")
    assert shop.shop_exists(folder) and session.role == "owner" and len(recovery.replace("-", "")) == 24
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    conn.close()
    raw = (folder / "shop.db").read_bytes()
    assert b"SQLite format" not in raw and b"anu" not in raw  # the file really is encrypted
    with pytest.raises(WrongPasswordError):
        open_db(folder / "shop.db", "owner-password")  # the password is NOT the database key


def test_cannot_set_up_twice_and_bad_input_leaves_nothing_behind(folder):
    with pytest.raises(ValueError, match="at least 8"):
        shop.setup_new_shop(folder, "anu", "short")
    assert not shop.shop_exists(folder)
    shop.setup_new_shop(folder, "anu", "owner-password")[0].close()
    with pytest.raises(ValueError, match="already exists"):
        shop.setup_new_shop(folder, "anu", "owner-password")


def test_sign_in_and_wrong_password(folder):
    shop.setup_new_shop(folder, "anu", "owner-password")[0].close()
    conn, session = shop.open_shop(folder, "anu", "owner-password")
    assert session.username == "anu" and session.role == "owner"
    conn.close()
    with pytest.raises(AuthError, match="Wrong username or password"):
        shop.open_shop(folder, "anu", "wrong-password")
    with pytest.raises(AuthError, match="Wrong username or password"):
        shop.open_shop(folder, "ghost", "owner-password")


def test_repeated_wrong_passwords_lock_sign_in(folder):
    shop.setup_new_shop(folder, "anu", "owner-password")[0].close()
    for _ in range(5):
        with pytest.raises(AuthError):
            shop.open_shop(folder, "anu", "wrong-password")
    with pytest.raises(AccountLocked):
        shop.open_shop(folder, "anu", "owner-password")


def test_owner_adds_a_cashier_who_can_sign_in(folder):
    conn, owner, _ = shop.setup_new_shop(folder, "anu", "owner-password")
    shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    conn.close()
    conn2, till = shop.open_shop(folder, "till", "till-password")
    assert till.role == "cashier"
    with pytest.raises(PermissionDenied):
        shop.add_user(folder, conn2, till, "x2", "another-pass", "cashier")  # cashier cannot add users
    assert keystore.slot_names(folder / "shop.keys") == ["anu", "till"]
    conn2.close()


def test_password_changes_keep_key_file_in_step(folder):
    conn, owner, _ = shop.setup_new_shop(folder, "anu", "owner-password")
    uid = shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    shop.change_my_password(folder, conn, owner, "owner-password", "owner-new-pass")
    shop.reset_user_password(folder, conn, owner, uid, "till-new-pass")
    conn.close()
    with pytest.raises(AuthError):
        shop.open_shop(folder, "anu", "owner-password")
    shop.open_shop(folder, "anu", "owner-new-pass")[0].close()
    with pytest.raises(AuthError):
        shop.open_shop(folder, "till", "till-password")
    shop.open_shop(folder, "till", "till-new-pass")[0].close()


def test_deactivating_removes_access_and_reactivating_needs_new_password(folder):
    conn, owner, _ = shop.setup_new_shop(folder, "anu", "owner-password")
    uid = shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    shop.set_user_active(folder, conn, owner, uid, False)
    with pytest.raises(AuthError):
        shop.open_shop(folder, "till", "till-password")
    with pytest.raises(ValueError, match="new password"):
        shop.set_user_active(folder, conn, owner, uid, True)
    shop.set_user_active(folder, conn, owner, uid, True, new_password="till-again-pw")
    conn.close()
    shop.open_shop(folder, "till", "till-again-pw")[0].close()


def test_locked_session_blocks_user_management(folder):
    conn, owner, _ = shop.setup_new_shop(folder, "anu", "owner-password")
    owner.lock()
    with pytest.raises(SessionLocked):
        shop.add_user(folder, conn, owner, "till", "till-password", "cashier")
    conn.close()


def test_recovery_key_restores_access_and_is_replaced(folder):
    conn, owner, recovery = shop.setup_new_shop(folder, "anu", "owner-password")
    conn.execute("INSERT INTO products(name) VALUES ('Shirt')")
    conn.close()
    with pytest.raises(AuthError, match="Wrong recovery key"):
        shop.recover_access(folder, "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF", "brand-new-pass")
    conn2, session, new_recovery = shop.recover_access(folder, recovery, "brand-new-pass")
    assert session.role == "owner" and new_recovery != recovery
    assert conn2.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1  # data survived
    assert conn2.execute("SELECT 1 FROM audit_log WHERE action = 'recovery_key_used'").fetchone()
    conn2.close()
    with pytest.raises(AuthError):
        shop.open_shop(folder, "anu", "owner-password")
    shop.open_shop(folder, "anu", "brand-new-pass")[0].close()
    with pytest.raises(AuthError):
        shop.recover_access(folder, recovery, "another-pass-1")  # the old recovery key is dead


def test_missing_key_file_cannot_open_shop(folder):
    shop.setup_new_shop(folder, "anu", "owner-password")[0].close()
    (folder / "shop.keys").unlink()
    with pytest.raises(AuthError, match="missing or damaged"):
        shop.open_shop(folder, "anu", "owner-password")
