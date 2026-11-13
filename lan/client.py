"""The second counter's side: a stand-in for the database connection that works over the shop network.

RemoteConnection looks like the normal database connection (execute, executemany, commit, rollback, in_transaction,
rows you can read by number or by name), so all of core/ and ui/ run on it unchanged.
"""

import http.client
import itertools
import socket

from sqlcipher3 import dbapi2 as sqlcipher

from lan.protocol import (DEFAULT_PORT, LanError, check_private_address, derive_key, pack_value, seal, unpack_value,
                          unseal)

TIMEOUT_SECONDS = 20


class RemoteRow(tuple):
    """A row you can read like a tuple or by column name (any capitalisation), like sqlite3.Row."""

    def __new__(cls, cols, values):
        row = super().__new__(cls, values)
        row._cols = list(cols)
        row._index = {c.lower(): i for i, c in enumerate(cols)}
        return row

    def keys(self):
        return list(self._cols)

    def __getitem__(self, item):
        if isinstance(item, str):
            try:
                return super().__getitem__(self._index[item.lower()])
            except KeyError:
                raise IndexError("No item with that key") from None
        return super().__getitem__(item)


class RemoteCursor:
    def __init__(self, reply):
        cols = reply.get("cols", [])
        self.description = [(c, None, None, None, None, None, None) for c in cols] or None
        self._rows = [RemoteRow(cols, [unpack_value(v) for v in row]) for row in reply.get("rows", [])]
        self._pos = 0
        self.lastrowid = reply.get("lastrowid")
        self.rowcount = reply.get("rowcount", -1)

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self):
        rest, self._pos = self._rows[self._pos:], len(self._rows)
        return rest

    def fetchmany(self, size=1):
        rest = self._rows[self._pos:self._pos + size]
        self._pos += len(rest)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class RemoteConnection:
    isolation_level = None

    def __init__(self, host, port, pairing_code):
        self.host = check_private_address(host)
        self.port = int(port)
        self._key = derive_key(pairing_code)
        self._seq = itertools.count(1)
        self._sid = None
        self._in_tx = False
        self.row_factory = None                   # rows are always RemoteRow
        self._open()

    # ------------------------------------------------------------------ transport

    def _post(self, message) -> dict:
        message["seq"] = next(self._seq)
        try:
            http_conn = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT_SECONDS)
            try:
                http_conn.request("POST", "/rpc", body=seal(self._key, message),
                                  headers={"Content-Type": "application/octet-stream"})
                response = http_conn.getresponse()
                body = response.read()
            finally:
                http_conn.close()
        except (OSError, http.client.HTTPException, socket.timeout):
            raise LanError("The main counter cannot be reached. Check that its PC is on, the app is open and "
                           "'Second Counter' is started.") from None
        if response.status == 403:
            raise LanError("The main counter refused the connection. Check the pairing code.")
        if response.status != 200:
            raise LanError("The main counter did not answer properly.")
        return unseal(self._key, body)

    def _open(self) -> None:
        self._sid = self._post({"op": "open"})["sid"]

    def _call(self, message) -> dict:
        reply = self._post({**message, "sid": self._sid})
        if reply.get("ok"):
            self._in_tx = bool(reply.get("in_tx", self._in_tx))
            return reply
        etype, text = reply.get("etype", "OperationalError"), reply.get("msg", "")
        if etype == "LanError":
            raise LanError(text)
        raise getattr(sqlcipher, etype, sqlcipher.OperationalError)(text)

    # ------------------------------------------------------------------ the connection interface

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    def execute(self, sql, params=()):
        word = str(sql).lstrip().split(None, 1)[0].upper() if str(sql).strip() else ""
        try:
            reply = self._call({"op": "exec", "sql": sql, "params": [pack_value(p) for p in params]})
        except LanError:
            if word in ("ROLLBACK", "END"):        # best effort: the main counter rolls back an abandoned save itself
                self._in_tx = False
                return RemoteCursor({})
            raise
        if word in ("COMMIT", "END", "ROLLBACK"):
            self._in_tx = False
        return RemoteCursor(reply)

    def executemany(self, sql, rows):
        reply = self._call({"op": "many", "sql": sql, "rows": [[pack_value(v) for v in row] for row in rows]})
        return RemoteCursor(reply)

    def commit(self) -> None:
        if self._in_tx:
            self.execute("COMMIT")

    def rollback(self) -> None:
        if self._in_tx:
            self.execute("ROLLBACK")

    def close(self) -> None:
        if self._sid is None:
            return
        sid, self._sid = self._sid, None
        try:
            self._post({"op": "close", "sid": sid})
        except LanError:
            pass


def connect_counter(host, port, pairing_code, username, password):
    """Sign in on the main counter from a second PC. Returns (conn, session), like core.shop.open_shop."""
    from core import auth
    from core.session import Session, auto_lock_minutes

    conn = RemoteConnection(host, port or DEFAULT_PORT, pairing_code)
    try:
        user = auth.authenticate(conn, username, password)
        return conn, Session(user, timeout_minutes=auto_lock_minutes(conn), db_key=None)
    except Exception:
        conn.close()
        raise
