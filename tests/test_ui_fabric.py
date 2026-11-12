"""The Fabric screen."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import fabric  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from ui.screens import fabric_screen  # noqa: E402
from tests.test_bill_document import shop  # noqa: E402,F401


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)


def test_receive_a_roll_and_set_the_shortest_cut(shop):
    pid = create_product(shop.conn, "Cotton cloth", hsn="6205", default_price_paise=25000, unit="metre")
    (vid,) = generate_variants(shop.conn, pid, ["Free"], ["White"])
    session = SimpleNamespace(user_id=shop.user, touch=lambda: None, require=lambda *a, **k: None)
    s = fabric_screen.build(shop.conn, session, None)
    s.txt_length.setText("20")
    s.txt_ref.setText("R1")
    s.receive()
    assert s.tbl.rowCount() == 1 and s.tbl.item(0, 3).text() == "20 m"
    s.txt_length.setText("abc")                         # a bad length adds nothing
    s.receive()
    assert s.tbl.rowCount() == 1
    s.txt_min.setText("0.5")
    s.save_min()
    assert fabric.metre_products(shop.conn, shop.user)[0]["min_cut_cm"] == 50
