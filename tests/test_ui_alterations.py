"""The Alterations screen."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import alterations as alt  # noqa: E402
from ui.screens import alterations_screen  # noqa: E402
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
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)


def _fill(s):
    s.txt_name.setText("Ravi")
    s.txt_item.setText("Blue shirt")
    s.txt_work.setText("Shorten sleeves")
    s.txt_charge.setText("150")
    s.txt_advance.setText("50")


def test_take_a_job_and_move_it_to_delivered(shop, session):
    s = alterations_screen.build(shop.conn, session, None)
    _fill(s)
    s.save_job()
    jobs = alt.list_jobs(shop.conn, 1)
    assert len(jobs) == 1 and jobs[0]["charge_paise"] == 15000 and jobs[0]["advance_paise"] == 5000
    assert s.tbl.rowCount() == 1
    for status in ("in_progress", "ready", "delivered"):
        s.tbl.selectRow(0)
        s.next_step()
        assert alt.get_job(shop.conn, 1, jobs[0]["id"])["status"] == status
        if status != "delivered":
            assert s.btn_next.isEnabled()
    s.tbl.selectRow(0)
    assert not s.btn_next.isEnabled() and not s.btn_cancel.isEnabled()


def test_bad_input_saves_nothing(shop, session):
    s = alterations_screen.build(shop.conn, session, None)
    _fill(s)
    s.txt_advance.setText("500")                       # more than the charge
    s.save_job()
    assert alt.list_jobs(shop.conn, 1) == []


def test_ready_filter_and_cancel(shop, session):
    s = alterations_screen.build(shop.conn, session, None)
    _fill(s)
    s.save_job()
    jid = alt.list_jobs(shop.conn, 1)[0]["id"]
    alt.set_status(shop.conn, 1, jid, "ready")
    s.cmb_filter.setCurrentIndex([i for i in range(s.cmb_filter.count()) if s.cmb_filter.itemData(i) == "ready"][0])
    assert s.tbl.rowCount() == 1
    s.tbl.selectRow(0)
    s.cancel_job()
    assert alt.get_job(shop.conn, 1, jid)["status"] == "cancelled"
