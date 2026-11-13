"""The main counter's side: lets a second counter on the shop network use the shop database.

Started by the owner from the "Second Counter" menu, never automatically. Each second counter gets its own
connection to the same encrypted database, so two counters billing at the same moment are handled by the database
itself (one saves, the other waits a moment), exactly as two screens on one PC would be.
"""

import http.server
import json
import logging
import secrets
import threading
import time

from sqlcipher3 import dbapi2 as sqlcipher

from lan.protocol import (LanError, check_private_address, check_statement, derive_key, pack_value, seal, unpack_value,
                          unseal)

log = logging.getLogger("clothshop")

MAX_BODY = 8 * 1024 * 1024
IDLE_SECONDS = 12 * 3600          # a counter that is signed in but quiet keeps its place
IDLE_IN_TRANSACTION_SECONDS = 60  # a counter that stopped in the middle of saving must not block the other one
BAD_TRIES_BEFORE_BLOCK = 10
BLOCK_SECONDS = 300


class _Session:
    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.last_seq = -1
        self.touched = time.monotonic()


def _open_for_session(db_path, db_key):
    conn = sqlcipher.connect(str(db_path), check_same_thread=False)
    conn.isolation_level = None
    escaped = db_key.replace("'", "''")
    conn.execute(f"PRAGMA key = '{escaped}'")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    conn.row_factory = sqlcipher.Row
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


class CounterServer:
    def __init__(self, db_path, db_key, pairing_code, host, port=0):
        self.host = check_private_address(host)
        self.db_path, self._db_key = db_path, db_key
        self._key = derive_key(pairing_code)
        self._requested_port = port
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._bad = {}                       # ip -> (count, blocked_until)
        self._httpd = self._thread = self._reaper = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ run / stop

    @property
    def port(self):
        return self._httpd.server_address[1] if self._httpd else None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> None:
        if self._httpd is not None:
            return
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                                           # noqa: N802
                server._handle(self)

            def log_message(self, *args):                                # no console noise, no customer data in logs
                return

        self._httpd = http.server.ThreadingHTTPServer((self.host, self._requested_port), Handler)
        self._httpd.daemon_threads = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="counter-server", daemon=True)
        self._reaper = threading.Thread(target=self._reap_loop, name="counter-reaper", daemon=True)
        self._thread.start()
        self._reaper.start()
        log.info("Second counter server started on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        with self._sessions_lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for s in sessions:
            self._close_session(s)
        log.info("Second counter server stopped")

    def session_count(self) -> int:
        with self._sessions_lock:
            return len(self._sessions)

    # ------------------------------------------------------------------ one request

    def _blocked(self, ip) -> bool:
        count, until = self._bad.get(ip, (0, 0))
        return until > time.monotonic()

    def _note_bad(self, ip) -> None:
        count, until = self._bad.get(ip, (0, 0))
        count += 1
        if count >= BAD_TRIES_BEFORE_BLOCK:
            self._bad[ip] = (0, time.monotonic() + BLOCK_SECONDS)
            log.warning("Second counter: too many wrong codes from %s; blocked for %s s", ip, BLOCK_SECONDS)
        else:
            self._bad[ip] = (count, 0)

    def _handle(self, handler) -> None:
        ip = handler.client_address[0]
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if self._blocked(ip) or length <= 0 or length > MAX_BODY or handler.path != "/rpc":
                handler.send_response(403)
                handler.end_headers()
                return
            body = handler.rfile.read(length)
            try:
                request = unseal(self._key, body)
            except LanError:
                self._note_bad(ip)
                handler.send_response(403)
                handler.end_headers()
                return
            reply = seal(self._key, self._dispatch(request))
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(reply)))
            handler.end_headers()
            handler.wfile.write(reply)
        except (OSError, ValueError):
            return

    def _dispatch(self, req) -> dict:
        op, seq = req.get("op"), req.get("seq")
        try:
            if op == "ping":
                return {"ok": True, "seq": seq}
            if op == "open":
                conn = _open_for_session(self.db_path, self._db_key)
                sid = secrets.token_hex(16)
                with self._sessions_lock:
                    self._sessions[sid] = _Session(conn)
                return {"ok": True, "sid": sid, "seq": seq}
            with self._sessions_lock:
                session = self._sessions.get(req.get("sid"))
            if session is None:
                return {"ok": False, "etype": "LanError", "msg": "The connection to the main counter was lost. Sign in again.", "seq": seq}
            with session.lock:
                if not isinstance(seq, int) or seq <= session.last_seq:
                    return {"ok": False, "etype": "LanError", "msg": "Out-of-order message refused.", "seq": seq}
                session.last_seq = seq
                session.touched = time.monotonic()
                if op == "close":
                    with self._sessions_lock:
                        self._sessions.pop(req.get("sid"), None)
                    self._close_session(session, locked=True)
                    return {"ok": True, "seq": seq}
                if op in ("exec", "many"):
                    return self._execute(session, req, many=(op == "many"))
            return {"ok": False, "etype": "LanError", "msg": "Unknown request.", "seq": seq}
        except sqlcipher.Error as exc:
            return {"ok": False, "etype": type(exc).__name__, "msg": str(exc), "seq": seq}
        except LanError as exc:
            return {"ok": False, "etype": "LanError", "msg": str(exc), "seq": seq}
        except Exception:                                                # never let one request stop the server
            log.exception("Second counter: request failed")
            return {"ok": False, "etype": "OperationalError", "msg": "The main counter could not do that.", "seq": seq}

    def _execute(self, session, req, many) -> dict:
        sql = req.get("sql", "")
        check_statement(sql)
        conn = session.conn
        if many:
            batch = [[unpack_value(v) for v in row] for row in req.get("rows", [])]
            cur = conn.executemany(sql, batch)
        else:
            cur = conn.execute(sql, [unpack_value(v) for v in req.get("params", [])])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [[pack_value(v) for v in row] for row in cur.fetchall()] if cols else []
        return {"ok": True, "cols": cols, "rows": rows, "lastrowid": cur.lastrowid, "rowcount": cur.rowcount,
                "in_tx": conn.in_transaction, "seq": req.get("seq")}

    # ------------------------------------------------------------------ housekeeping

    @staticmethod
    def _close_session(session, locked=False) -> None:
        def work():
            try:
                if session.conn.in_transaction:
                    session.conn.execute("ROLLBACK")
                session.conn.close()
            except sqlcipher.Error:
                pass
        if locked:
            work()
        else:
            with session.lock:
                work()

    def _reap_loop(self) -> None:
        while not self._stop.wait(5):
            now = time.monotonic()
            stale = []
            with self._sessions_lock:
                for sid, s in list(self._sessions.items()):
                    limit = IDLE_IN_TRANSACTION_SECONDS if self._in_tx(s) else IDLE_SECONDS
                    if now - s.touched > limit:
                        stale.append(self._sessions.pop(sid))
            for s in stale:
                self._close_session(s)

    @staticmethod
    def _in_tx(session) -> bool:
        if not session.lock.acquire(blocking=False):
            return False                       # busy right now, so not idle
        try:
            return session.conn.in_transaction
        finally:
            session.lock.release()
