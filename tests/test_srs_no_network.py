"""SRS section 9 (Security) and section 10 (Privacy): the application makes no outbound connection.

The SRS test is "capture network traffic for a full working day". These tests check the same thing from
inside, two ways that do not depend on a quiet network:

  1. Static: no source file imports a network library (sockets, HTTP, FTP, mail, Qt network or web engine),
     and the requirements file lists no HTTP client.
  2. Runtime: the whole shop is run (set up, products, purchase stock, bill, hold, labels, backup, restore
     check, every screen of the main window) while every attempt to open a network connection is caught.
     There must be none.

A network capture on the real shop PC for a working day (Wireshark or the Windows firewall log) is still
the final proof on the day of delivery; these tests keep it from ever regressing before then.
"""

import ast
import os
import socket
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("core", "ui", "db", "app")

# a module name matches if it is one of these or lives inside one of these packages
BANNED = {
    "socket", "ssl", "http", "urllib.request", "urllib3", "requests", "httpx", "aiohttp", "websocket", "websockets",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "socketserver", "asyncio.streams",
    "PySide6.QtNetwork", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
    "PySide6.QtWebChannel", "PySide6.QtWebEngineQuick",
}
BANNED_REQUIREMENTS = ("requests", "httpx", "urllib3", "aiohttp", "websocket", "pycurl", "paramiko", "boto", "firebase",
                       "sentry", "google-cloud", "twilio")


def _is_banned(name: str) -> bool:
    return any(name == b or name.startswith(b + ".") for b in BANNED)


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))   # some files start with a BOM
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module, node.lineno
            for alias in node.names:                    # from PySide6 import QtNetwork
                yield f"{node.module}.{alias.name}", node.lineno


def _source_files():
    for folder in SOURCE_DIRS:
        base = ROOT / folder
        if base.exists():
            yield from (p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    yield from (p for p in ROOT.glob("*.py") if p.name not in {"make_zip.py"})


def test_no_source_file_imports_a_network_library():
    found = [f"{path.relative_to(ROOT)}:{line}  imports {name}"
             for path in _source_files() for name, line in _imports(path) if _is_banned(name)]
    assert found == [], "network libraries are not allowed:\n" + "\n".join(found)


def test_the_requirements_list_no_network_client():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    names = [line.split("#")[0].strip() for line in text.splitlines()]
    names = [n.replace("==", " ").replace(">=", " ").replace("<=", " ").split(" ")[0] for n in names if n]
    bad = [n for n in names if any(n.startswith(b) for b in BANNED_REQUIREMENTS)]
    assert bad == []


def test_the_scanner_itself_catches_a_banned_import(tmp_path):
    sample = tmp_path / "x.py"
    sample.write_text("import urllib.request\nfrom PySide6 import QtNetwork\nimport json\n", encoding="utf-8")
    assert [n for n, _ in _imports(sample) if _is_banned(n)] == ["urllib.request", "PySide6.QtNetwork"]


# ---------------------------------------------------------------- runtime

@pytest.fixture
def network_watch(monkeypatch):
    """Record every attempt to reach the network instead of making it."""
    attempts = []

    def spy(name):
        def caught(*args, **kwargs):
            attempts.append(f"{name} {args[1:] if name.startswith('socket.') else args}")
            raise OSError("the network is closed in tests")
        return caught

    for target in ("connect", "connect_ex", "sendto", "sendmsg", "bind", "listen"):
        if hasattr(socket.socket, target):          # sendmsg does not exist on Windows
            monkeypatch.setattr(socket.socket, target, spy(f"socket.{target}"))
    monkeypatch.setattr(socket, "create_connection", spy("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", spy("getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", spy("gethostbyname"))
    return attempts


def test_a_whole_working_day_makes_no_network_attempt(tmp_path, network_watch):
    from PySide6.QtWidgets import QApplication

    from core import backup, shop
    from core.sales import create_sale
    from core.products import create_product, generate_variants
    from core import stock_holds
    from core.stock import add_movement
    from core.shop_settings import update_shop_profile
    from db.connection import transaction
    from ui import main_window as mw
    from ui import print_service

    QApplication.instance() or QApplication([])
    folder = tmp_path / "shop"
    conn, owner, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
    update_shop_profile(conn, owner.user_id, name="Anu Cloth House", tax_scheme="unregistered")
    product = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(conn, product, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 20, "opening stock")
    create_sale(conn, [{"variant_id": variant, "qty": 2}], [{"mode": "cash", "amount_paise": 210000}],
                user_id=owner.user_id, on_date="2026-11-07")
    hold_id = stock_holds.place_hold(conn, owner.user_id, variant, 1, "trial", 3)
    stock_holds.release_hold(conn, owner.user_id, hold_id)
    session = owner
    assert print_service.save_labels_pdf(conn, session, [{"variant_id": variant, "copies": 2}],
                                         str(tmp_path / "labels.pdf")).ok
    made = backup.create_backup(conn, session, folder, tmp_path / "bk")
    backup.inspect_backup(made["path"], "owner", "owner-password")

    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House"}, folder=folder)
    for index in range(window.pages.count()):          # open every screen once
        window.menu.setCurrentRow(index)
    window.close()
    conn.close()

    assert network_watch == [], "the application tried to use the network: " + "; ".join(network_watch)


def test_the_watcher_really_catches_a_connection(network_watch):
    """If this stopped failing, a clean run of the test above would prove nothing."""
    with pytest.raises(OSError):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(OSError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("93.184.216.34", 80))
    assert len(network_watch) == 2