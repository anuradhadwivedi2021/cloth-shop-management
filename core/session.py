"""A signed-in session with auto-lock.

The screen layer creates one Session after sign-in and calls `require(...)` or
`touch()` on every action. After `timeout_minutes` without activity the session
locks; unlocking needs the same user's password again.
The clock can be replaced in tests.
"""

import time

from core.auth import authenticate
from core.permissions import require_permission
from core.settings import get_setting

DEFAULT_AUTO_LOCK_MINUTES = 5


class SessionLocked(Exception):
    """The session is locked (idle timeout or manual lock). Ask for the password."""


def auto_lock_minutes(conn) -> int:
    raw = get_setting(conn, "auto_lock_minutes")
    try:
        minutes = int(str(raw).strip()) if raw is not None else DEFAULT_AUTO_LOCK_MINUTES
    except ValueError:
        return DEFAULT_AUTO_LOCK_MINUTES
    return minutes if minutes >= 1 else DEFAULT_AUTO_LOCK_MINUTES


class Session:
    def __init__(self, user: dict, *, timeout_minutes: int = DEFAULT_AUTO_LOCK_MINUTES, db_key=None, clock=time.monotonic):
        if timeout_minutes < 1:
            raise ValueError("Auto-lock time must be at least 1 minute.")
        self.user_id = user["id"]
        self.username = user["username"]
        self.role = user["role"]
        self.db_key = db_key  # needed to add users to the key file; kept only in memory
        self._timeout = timeout_minutes * 60
        self._clock = clock
        self._last = clock()
        self._manual = False

    @property
    def timeout_minutes(self) -> int:
        return self._timeout // 60

    def set_timeout_minutes(self, minutes: int) -> None:
        """Change the idle time before locking for this signed-in session (used right after setup)."""
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            raise ValueError("Auto-lock time must be at least 1 minute.")
        self._timeout = minutes * 60

    @property
    def locked(self) -> bool:
        return self._manual or (self._clock() - self._last) >= self._timeout

    def touch(self) -> None:
        if self.locked:
            raise SessionLocked("Session is locked.")
        self._last = self._clock()

    def lock(self) -> None:
        self._manual = True

    def unlock(self, conn, password) -> None:
        user = authenticate(conn, self.username, password)
        if user["id"] != self.user_id:
            raise SessionLocked("Wrong account.")
        self._manual = False
        self._last = self._clock()

    def require(self, conn, permission: str, action=None):
        """Touch the session and check the permission against the database right now."""
        self.touch()
        return require_permission(conn, self.user_id, permission, action)
