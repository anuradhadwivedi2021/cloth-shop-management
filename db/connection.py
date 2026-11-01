"""Encrypted SQLCipher connection and transaction helper."""

from contextlib import contextmanager
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

from db.schema import migrate


class WrongPasswordError(Exception):
    """Raised when the password is wrong or the file is not a valid database."""


def open_db(path, password: str):
    """Open (or create) the encrypted database and bring the schema up to date.

    The connection is in autocommit mode; use `transaction(conn)` for every
    business action so it is saved completely or not at all.
    """
    conn = sqlcipher.connect(str(Path(path)))
    conn.isolation_level = None  # we control transactions explicitly
    try:
        escaped = password.replace("'", "''")
        conn.execute(f"PRAGMA key = '{escaped}'")
        # First read fails here if the key is wrong
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher.DatabaseError as exc:
        conn.close()
        raise WrongPasswordError("Wrong password or invalid database file.") from exc

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlcipher.Row
    migrate(conn)
    return conn


@contextmanager
def transaction(conn):
    """One atomic business action: everything is saved, or nothing is.

    Usage:
        with transaction(conn):
            conn.execute(...)
            conn.execute(...)
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")