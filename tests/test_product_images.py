"""PRD-07: product pictures stored in the encrypted database, with size, count and type limits."""

import os
import struct
import zlib
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import auth, product_edit as pe, product_images as pi, shop  # noqa: E402


def _png(path, side=8):
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * side for _ in range(side))
    def chunk(k, d):
        return struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


@pytest.fixture
def world(tmp_path):
    conn, owner, _ = shop.setup_new_shop(tmp_path / "shop", "owner", "owner-password", pin="1234")
    pid = pe.add_product(conn, owner.user_id, "Shirt", default_price_paise=100000)["product_id"]
    yield conn, owner, pid, tmp_path
    conn.close()


def test_add_list_get_and_remove(world):
    conn, owner, pid, tmp = world
    img = pi.add_image(conn, owner.user_id, pid, _png(tmp / "front.png"))
    rows = pi.list_images(conn, owner.user_id, pid)
    assert [(r["file_name"], r["mime"]) for r in rows] == [("front.png", "image/png")]
    got = pi.get_image(conn, owner.user_id, img)
    assert got["data"][:8] == b"\x89PNG\r\n\x1a\n" and got["mime"] == "image/png"
    pi.remove_image(conn, owner.user_id, img)
    assert pi.list_images(conn, owner.user_id, pid) == []
    with pytest.raises(ValueError, match="not found"):
        pi.get_image(conn, owner.user_id, img)


def test_limits_and_real_content_are_checked(world):
    conn, owner, pid, tmp = world
    fake = tmp / "fake.png"
    fake.write_bytes(b"this is not a picture")
    with pytest.raises(ValueError, match="PNG or JPEG"):
        pi.add_image(conn, owner.user_id, pid, fake)
    big = tmp / "big.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"0" * (pi.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="2 MB"):
        pi.add_image(conn, owner.user_id, pid, big)
    empty = tmp / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        pi.add_image(conn, owner.user_id, pid, empty)
    with pytest.raises(ValueError, match="could not be read"):
        pi.add_image(conn, owner.user_id, pid, tmp / "missing.png")
    jpg = tmp / "ok.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"1" * 100)
    pi.add_image(conn, owner.user_id, pid, jpg)                      # a JPEG header is accepted
    for i in range(3):
        pi.add_image(conn, owner.user_id, pid, _png(tmp / f"p{i}.png"))
    with pytest.raises(ValueError, match="at most 4"):
        pi.add_image(conn, owner.user_id, pid, _png(tmp / "fifth.png"))
    with pytest.raises(ValueError, match="Product not found"):
        pi.add_image(conn, owner.user_id, 9999, _png(tmp / "x.png"))


def test_pictures_cannot_be_edited_and_need_permission(world):
    conn, owner, pid, tmp = world
    img = pi.add_image(conn, owner.user_id, pid, _png(tmp / "a.png"))
    import sqlcipher3
    with pytest.raises(sqlcipher3.dbapi2.DatabaseError):
        conn.execute("UPDATE product_images SET file_name = 'x' WHERE id = ?", (img,))
    stock = auth.create_user(conn, owner.user_id, "stock1", "stock-password-1", role="stock")
    cashier = auth.create_user(conn, owner.user_id, "cash1", "cash-password-1", role="cashier")
    with pytest.raises(ValueError):
        pi.add_image(conn, cashier, pid, _png(tmp / "b.png"))
    with pytest.raises(ValueError):
        pi.remove_image(conn, cashier, img)
    assert pi.list_images(conn, cashier, pid)                          # a cashier may look


def test_the_panel_adds_limits_and_removes(world, monkeypatch):
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from ui.product_photos_panel import ProductPhotosPanel

    QApplication.instance() or QApplication([])
    conn, owner, pid, tmp = world
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    panel = ProductPhotosPanel(conn, SimpleNamespace(user_id=owner.user_id), pid)
    for i in range(4):
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, p=_png(tmp / f"q{i}.png"), **k: (str(p), ""))
        panel.add()
    assert panel.list.count() == 4 and not panel.add_btn.isEnabled()
    panel.list.setCurrentRow(0)
    panel.remove()
    assert panel.list.count() == 3 and panel.add_btn.isEnabled()
