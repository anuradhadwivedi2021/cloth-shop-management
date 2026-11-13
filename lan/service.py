"""One counter server for the whole app (the main PC runs at most one), plus small helpers for the screens."""

import json
import logging
import socket
import threading
from pathlib import Path

from core.permissions import has_permission, require_permission
from core.settings import get_setting, set_setting
from lan.protocol import DEFAULT_PORT, LanError, check_private_address, new_pairing_code
from lan.server import CounterServer

log = logging.getLogger("clothshop")
_lock = threading.Lock()
_server = None
LINK_FILE = "counter_link.json"


def local_addresses() -> list:
    """This PC's addresses on the shop network (private ones only), best guess first."""
    found = []
    try:                                                         # no packet is sent: it only asks which address would be used
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    out = []
    for address in found:
        try:
            check_private_address(address)
        except LanError:
            continue
        if address not in out and not address.startswith("127."):
            out.append(address)
    return out


def current():
    return _server


def pairing_code(conn) -> str:
    code = get_setting(conn, "lan_pairing_code")
    if not code:
        code = new_pairing_code()
        set_setting(conn, "lan_pairing_code", code)
    return code


def new_code(conn, user_id) -> str:
    require_permission(conn, user_id, "manage_settings", "change the pairing code")
    if _server is not None:
        raise LanError("Stop the second counter first, then make a new code.")
    code = new_pairing_code()
    set_setting(conn, "lan_pairing_code", code)
    return code


def start(conn, session, folder, host=None, port=DEFAULT_PORT):
    """Start sharing this shop with a second counter. Needs the owner-level 'manage_settings' permission."""
    global _server
    from core.shop import database_path
    require_permission(conn, session.user_id, "manage_settings", "start the second counter")
    if not session.db_key:
        raise LanError("This counter cannot share the shop. Start it on the main PC.")
    host = host or (local_addresses() or [None])[0]
    if host is None:
        raise LanError("This PC is not on a shop network (no private address found).")
    with _lock:
        if _server is not None:
            return _server
        server = CounterServer(database_path(folder), session.db_key, pairing_code(conn), host, port)
        server.start()
        _server = server
    return server


def stop() -> None:
    global _server
    with _lock:
        server, _server = _server, None
    if server is not None:
        server.stop()


def autostart(conn, session, folder) -> None:
    """Called after sign-in on the main PC: start sharing when the owner asked for that."""
    try:
        if get_setting(conn, "lan_autostart") == "1" and has_permission(conn, session.user_id, "manage_settings"):
            start(conn, session, folder)
    except Exception:
        log.exception("E-LAN: the second counter could not start by itself")


# ---------------------------------------------------------------- the second PC remembers where the main PC is

def load_link(folder) -> dict:
    try:
        data = json.loads((Path(folder) / LINK_FILE).read_text(encoding="utf-8"))
        return {"host": str(data.get("host", "")), "port": int(data.get("port", DEFAULT_PORT)), "code": str(data.get("code", ""))}
    except (OSError, ValueError):
        return {"host": "", "port": DEFAULT_PORT, "code": ""}


def save_link(folder, host, port, code) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LINK_FILE).write_text(json.dumps({"host": host, "port": int(port), "code": code}), encoding="utf-8")
