"""Opening a shop folder: key file + encrypted database + sign-in, in one place.

A shop folder holds:
    shop.db    the encrypted database
    shop.keys  the key file (see core/keystore.py)

  setup_new_shop   first run: creates both, the owner, and the recovery key
                   (show it ONCE and have the owner print / write it down).
  open_shop        sign in: unlock the key file, open the database, check the password.
  add_user         owner adds a user (database row + key slot).
  change_my_password / reset_user_password / set_user_active keep the key file in step.
  recover_access   forgotten owner password: use the recovery key, set a new password.
"""

from pathlib import Path

from core import auth, keystore
from core.audit import log_event
from core.permissions import require_permission
from core.session import Session, auto_lock_minutes
from db.connection import open_db, transaction

DB_FILE = "shop.db"
KEY_FILE = "shop.keys"


def _paths(folder):
    folder = Path(folder)
    return folder / DB_FILE, folder / KEY_FILE


def shop_exists(folder) -> bool:
    db, keys = _paths(folder)
    return db.exists() and keys.exists()


def setup_new_shop(folder, username, password, *, pin=None, full_name=None):
    """Create a new shop. Returns (conn, session, recovery_key)."""
    folder = Path(folder)
    db_path, key_path = _paths(folder)
    if db_path.exists() or key_path.exists():
        raise ValueError("A shop already exists in this folder.")
    auth.validate_password(password)
    if pin:
        auth.validate_pin(pin)
    auth._clean_username(username)
    folder.mkdir(parents=True, exist_ok=True)
    db_key, recovery = keystore.create_keystore(key_path, username, password)
    conn = open_db(db_path, db_key)
    try:
        auth.setup_first_owner(conn, username, password, pin=pin, full_name=full_name)
        user = auth.authenticate(conn, username, password)
    except Exception:
        conn.close()
        db_path.unlink(missing_ok=True)
        key_path.unlink(missing_ok=True)
        raise
    session = Session(user, timeout_minutes=auto_lock_minutes(conn), db_key=db_key)
    return conn, session, recovery


def open_shop(folder, username, password):
    """Sign in. Returns (conn, session). Raises AuthError / AccountLocked / KeystoreLocked."""
    db_path, key_path = _paths(folder)
    try:
        db_key = keystore.unlock_keystore(key_path, username, password)
    except keystore.KeystoreLocked as exc:
        raise auth.AccountLocked(exc.seconds) from None
    except keystore.KeystoreError as exc:
        raise auth.AuthError(str(exc)) from None
    conn = open_db(db_path, db_key)
    try:
        user = auth.authenticate(conn, username, password)
    except Exception:
        conn.close()
        raise
    return conn, Session(user, timeout_minutes=auto_lock_minutes(conn), db_key=db_key)


def add_user(folder, conn, session, username, password, role, *, pin=None, full_name=None, discount_cap_percent=None) -> int:
    """Owner adds a user: database row and key slot. Returns the new user id."""
    _, key_path = _paths(folder)
    session.require(conn, "manage_users", "add users")
    uid = auth.create_user(
        conn, session.user_id, username, password, role,
        pin=pin, full_name=full_name, discount_cap_percent=discount_cap_percent,
    )
    keystore.set_slot(key_path, session.db_key, username, password)
    return uid


def change_my_password(folder, conn, session, old_password, new_password) -> None:
    _, key_path = _paths(folder)
    session.touch()
    auth.change_password(conn, session.username, old_password, new_password)
    keystore.set_slot(key_path, session.db_key, session.username, new_password)


def reset_user_password(folder, conn, session, user_id, new_password) -> None:
    """Owner sets a new password for another user."""
    _, key_path = _paths(folder)
    session.require(conn, "manage_users", "reset passwords")
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("User not found.")
    auth.reset_password(conn, session.user_id, user_id, new_password)
    keystore.set_slot(key_path, session.db_key, row["username"], new_password)


def set_user_active(folder, conn, session, user_id, active: bool, *, new_password=None) -> None:
    """Deactivate a user (their key slot is removed). Reactivating needs a new password."""
    _, key_path = _paths(folder)
    session.require(conn, "manage_users", "change user status")
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("User not found.")
    if active and not new_password:
        raise ValueError("Set a new password to reactivate this user.")
    auth.set_user_active(conn, session.user_id, user_id, active)
    if active:
        auth.reset_password(conn, session.user_id, user_id, new_password)
        keystore.set_slot(key_path, session.db_key, row["username"], new_password)
    else:
        keystore.remove_slot(key_path, row["username"])


def recover_access(folder, recovery_key, new_password, *, owner_username=None):
    """Forgotten owner password. Returns (conn, session, new_recovery_key); the old recovery key stops working."""
    db_path, key_path = _paths(folder)
    try:
        db_key = keystore.unlock_keystore(key_path, keystore.RECOVERY_SLOT, recovery_key)
    except keystore.KeystoreLocked as exc:
        raise auth.AccountLocked(exc.seconds) from None
    except keystore.KeystoreError as exc:
        raise auth.AuthError(str(exc)) from None
    auth.validate_password(new_password)
    conn = open_db(db_path, db_key)
    try:
        if owner_username:
            owner = conn.execute(
                "SELECT id, username FROM users WHERE username = ? AND role = 'owner' AND is_active = 1", (owner_username,)
            ).fetchone()
        else:
            owner = conn.execute(
                "SELECT id, username FROM users WHERE role = 'owner' AND is_active = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        if owner is None:
            raise ValueError("No active owner found.")
        auth.reset_password_unchecked(conn, owner["id"], new_password, event="password_reset_by_recovery_key")
        keystore.set_slot(key_path, db_key, owner["username"], new_password)
        new_recovery = keystore.rotate_recovery_key(key_path, db_key)
        with transaction(conn):
            log_event(conn, "recovery_key_used", user_id=owner["id"], entity="user", entity_id=owner["id"])
        user = auth.authenticate(conn, owner["username"], new_password)
    except Exception:
        conn.close()
        raise
    return conn, Session(user, timeout_minutes=auto_lock_minutes(conn), db_key=db_key), new_recovery
