"""Users, passwords, owner PIN and lock-out.

  * Passwords and PINs are stored only as Argon2id hashes, never readable.
  * 5 wrong passwords lock the account: 30 s, then doubling, at most 15 minutes.
    Wrong-username and wrong-password give the same message.
  * The owner PIN approves discounts above a cashier's limit and return-policy
    overrides at the counter. Wrong PINs are throttled the same way.
  * Passwords and PINs never go into the audit log.
  * Every sign-in, failure, password or PIN change is written to the audit log.

`now` can be passed in tests; normally the current time is used.
"""

import math
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlcipher3 import dbapi2 as sqlcipher

from core.audit import log_event
from core.permissions import require_permission
from db.connection import transaction

_ph = PasswordHasher()

MIN_PASSWORD_LEN = 8
MAX_FAILED = 5
BASE_LOCK_SECONDS = 30
MAX_LOCK_SECONDS = 900
_FMT = "%Y-%m-%d %H:%M:%S"
ROLES = ("owner", "cashier", "stock", "accountant")


class AuthError(Exception):
    """Sign-in or PIN check failed."""


class AccountLocked(AuthError):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"Too many wrong attempts. Try again in {seconds} seconds.")


# ---------- small helpers ----------

def _now(now):
    return now or datetime.now()


def _remaining(locked_until, now) -> int:
    """Whole seconds left of a lock (0 when not locked)."""
    if not locked_until:
        return 0
    left = (datetime.strptime(locked_until, _FMT) - now).total_seconds()
    return math.ceil(left) if left > 0 else 0


def _lock_seconds(failures: int) -> int:
    if failures < MAX_FAILED:
        return 0
    return min(BASE_LOCK_SECONDS * 2 ** (failures - MAX_FAILED), MAX_LOCK_SECONDS)


def _verify(stored_hash, secret) -> bool:
    if not stored_hash:
        _ph.hash(str(secret or ""))  # same work as a real check, so timing does not reveal anything
        return False
    try:
        return _ph.verify(stored_hash, secret or "")
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


def validate_password(password) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN or not password.strip():
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    return password


def validate_pin(pin) -> str:
    if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 6:
        raise ValueError("PIN must be 4 to 6 digits.")
    return pin


def _clean_username(username) -> str:
    username = (username or "").strip()
    if len(username) < 2:
        raise ValueError("Username must be at least 2 characters.")
    return username


def _public(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "full_name": row["full_name"]}


# ---------- creating users ----------

def _insert_user(conn, username, password, role, pin, full_name, discount_cap_percent) -> int:
    username = _clean_username(username)
    validate_password(password)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    pin_hash = _ph.hash(validate_pin(pin)) if pin else None
    try:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, pin_hash, role, full_name, discount_cap_percent) "
            "VALUES (?,?,?,?,?,?)",
            (username, _ph.hash(password), pin_hash, role, (full_name or "").strip() or None, discount_cap_percent),
        )
    except sqlcipher.IntegrityError:
        raise ValueError("That username is already taken.") from None
    return cur.lastrowid


def needs_setup(conn) -> bool:
    """True on a brand-new shop: no user exists yet, so the owner must be created."""
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def setup_first_owner(conn, username, password, *, pin=None, full_name=None) -> int:
    """First-time setup: create the owner. Refused once any user exists."""
    with transaction(conn):
        if not needs_setup(conn):
            raise ValueError("Setup is already done.")
        uid = _insert_user(conn, username, password, "owner", pin, full_name, None)
        log_event(conn, "owner_created", user_id=uid, entity="user", entity_id=uid, after={"username": username.strip()})
        return uid


def create_user(conn, admin_id, username, password, role, *, pin=None, full_name=None, discount_cap_percent=None) -> int:
    with transaction(conn):
        require_permission(conn, admin_id, "manage_users", "add users")
        uid = _insert_user(conn, username, password, role, pin, full_name, discount_cap_percent)
        log_event(
            conn, "user_created", user_id=admin_id, entity="user", entity_id=uid,
            after={"username": username.strip(), "role": role},
        )
        return uid


# ---------- sign in ----------

def authenticate(conn, username, password, *, now=None) -> dict:
    """Check username and password. Returns the user dict or raises AuthError / AccountLocked."""
    now = _now(now)
    username = (username or "").strip()
    outcome = None
    with transaction(conn):
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None or not row["is_active"]:
            _verify(None, password)
            log_event(conn, "login_failed", after={"username": username[:60]})
            outcome = ("fail", None)
        elif _remaining(row["locked_until"], now) > 0:
            log_event(conn, "login_blocked", user_id=row["id"], entity="user", entity_id=row["id"])
            outcome = ("locked", _remaining(row["locked_until"], now))
        elif _verify(row["password_hash"], password):
            new_hash = _ph.hash(password) if _ph.check_needs_rehash(row["password_hash"]) else row["password_hash"]
            conn.execute(
                "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ?, password_hash = ? "
                "WHERE id = ?",
                (now.strftime(_FMT), new_hash, row["id"]),
            )
            log_event(conn, "login", user_id=row["id"], entity="user", entity_id=row["id"])
            outcome = ("ok", _public(row))
        else:
            failures = row["failed_attempts"] + 1
            secs = _lock_seconds(failures)
            until = (now + timedelta(seconds=secs)).strftime(_FMT) if secs else None
            conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (failures, until, row["id"]))
            log_event(
                conn, "login_failed", user_id=row["id"], entity="user", entity_id=row["id"],
                after={"failed_attempts": failures, "locked_seconds": secs},
            )
            outcome = ("fail", None)
    kind, value = outcome
    if kind == "ok":
        return value
    if kind == "locked":
        raise AccountLocked(value)
    raise AuthError("Wrong username or password.")


# ---------- passwords ----------

def change_password(conn, username, old_password, new_password, *, now=None) -> None:
    """A user changes their own password (needs the old one)."""
    validate_password(new_password)
    user = authenticate(conn, username, old_password, now=now)
    with transaction(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (_ph.hash(new_password), user["id"]))
        log_event(conn, "password_changed", user_id=user["id"], entity="user", entity_id=user["id"])


def reset_password_unchecked(conn, user_id, new_password, *, by_user_id=None, event="password_reset") -> None:
    """Set a new password and clear any lock. Callers must have checked permission (or be recovery)."""
    validate_password(new_password)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise ValueError("User not found.")
        conn.execute(
            "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (_ph.hash(new_password), user_id),
        )
        log_event(conn, event, user_id=by_user_id, entity="user", entity_id=user_id)


def reset_password(conn, admin_id, user_id, new_password) -> None:
    """Owner sets a new password for another user."""
    require_permission(conn, admin_id, "manage_users", "reset passwords")
    reset_password_unchecked(conn, user_id, new_password, by_user_id=admin_id)


def unlock_account(conn, admin_id, user_id) -> None:
    with transaction(conn):
        require_permission(conn, admin_id, "manage_users", "unlock accounts")
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, "
            "pin_failed_attempts = 0, pin_locked_until = NULL WHERE id = ?",
            (user_id,),
        )
        log_event(conn, "account_unlocked", user_id=admin_id, entity="user", entity_id=user_id)


def set_user_active(conn, admin_id, user_id, active: bool) -> None:
    """Deactivate (never delete) or reactivate a user. History is kept."""
    with transaction(conn):
        require_permission(conn, admin_id, "manage_users", "change user status")
        target = conn.execute("SELECT id, role, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise ValueError("User not found.")
        if not active:
            if user_id == admin_id:
                raise ValueError("You cannot deactivate your own account.")
            if target["role"] == "owner" and conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'owner' AND is_active = 1 AND id != ?", (user_id,)
            ).fetchone()[0] == 0:
                raise ValueError("The last active owner cannot be deactivated.")
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
        log_event(
            conn, "user_activated" if active else "user_deactivated",
            user_id=admin_id, entity="user", entity_id=user_id,
        )


# ---------- PIN ----------

def set_pin(conn, username, password, pin, *, now=None) -> None:
    """A user sets or changes their own PIN (needs their password)."""
    validate_pin(pin)
    user = authenticate(conn, username, password, now=now)
    with transaction(conn):
        conn.execute(
            "UPDATE users SET pin_hash = ?, pin_failed_attempts = 0, pin_locked_until = NULL WHERE id = ?",
            (_ph.hash(pin), user["id"]),
        )
        log_event(conn, "pin_changed", user_id=user["id"], entity="user", entity_id=user["id"])


def verify_owner_pin(conn, pin, *, now=None) -> int:
    """Check an owner PIN typed at the counter. Returns the approving owner's id.

    A wrong PIN counts against every owner who has a PIN, so guessing is throttled.
    """
    now = _now(now)
    outcome = None
    with transaction(conn):
        owners = conn.execute(
            "SELECT * FROM users WHERE role = 'owner' AND is_active = 1 AND pin_hash IS NOT NULL ORDER BY id"
        ).fetchall()
        if not owners:
            outcome = ("none", None)
        else:
            open_owners = [o for o in owners if _remaining(o["pin_locked_until"], now) == 0]
            if not open_owners:
                outcome = ("locked", max(_remaining(o["pin_locked_until"], now) for o in owners))
            else:
                match = next((o for o in open_owners if _verify(o["pin_hash"], pin)), None)
                if match is not None:
                    conn.execute(
                        "UPDATE users SET pin_failed_attempts = 0, pin_locked_until = NULL WHERE id = ?", (match["id"],)
                    )
                    log_event(conn, "owner_pin_approved", user_id=match["id"], entity="user", entity_id=match["id"])
                    outcome = ("ok", match["id"])
                else:
                    for o in open_owners:
                        n = o["pin_failed_attempts"] + 1
                        secs = _lock_seconds(n)
                        until = (now + timedelta(seconds=secs)).strftime(_FMT) if secs else None
                        conn.execute(
                            "UPDATE users SET pin_failed_attempts = ?, pin_locked_until = ? WHERE id = ?",
                            (n, until, o["id"]),
                        )
                    log_event(conn, "owner_pin_failed")
                    outcome = ("fail", None)
    kind, value = outcome
    if kind == "ok":
        return value
    if kind == "locked":
        raise AccountLocked(value)
    if kind == "none":
        raise AuthError("No owner PIN has been set.")
    raise AuthError("Wrong PIN.")
