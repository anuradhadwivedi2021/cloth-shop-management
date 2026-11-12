"""The Commission Rates screen and the report entry."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import commission  # noqa: E402
from core.auth import create_user  # noqa: E402
from ui.screens import commission_screen, reports_screen  # noqa: E402
from tests.test_bill_document import shop  # noqa: E402,F401


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(shop):
    return SimpleNamespace(user_id=shop.user, touch=lambda: None, require=lambda *a, **k: None)


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)


def test_screen_sets_default_and_person_rate(shop, session):
    asha = create_user(shop.conn, 1, "asha", "Pass#12345", "cashier", full_name="Asha")
    s = commission_screen.build(shop.conn, session, None)
    s.toggle()
    assert commission.enabled(shop.conn)
    s.default_edit.setText("2")
    s.save_default()
    assert commission.default_bp(shop.conn) == 200
    row = [p["id"] for p in s._people].index(asha)
    s.tbl.selectRow(row)
    s.person_edit.setText("3.5")
    s.save_person()
    assert {p["id"]: p for p in commission.list_salespeople(shop.conn, 1)}[asha]["rate_bp"] == 350
    s.tbl.selectRow(row)
    s.clear_person()
    assert {p["id"]: p for p in commission.list_salespeople(shop.conn, 1)}[asha]["rate_bp"] == 200


def test_commission_report_is_listed():
    assert any(r.key == "commission" for r in reports_screen.REPORTS)
