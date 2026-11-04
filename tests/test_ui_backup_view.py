"""The Backup and Restore screen must use the real, tested backup code (not copy the file itself)."""

import gc
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import backup, shop  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.sales import create_sale  # noqa: E402
from core.stock import add_movement  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui.backup_view import BackupRestoreView, pretty_time  # noqa: E402

PASSWORD = "owner-password"
ROOT = Path(__file__).resolve().parent.parent


_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run (avoids a Qt exit crash)


def _dispose(widget):
    """Really delete a widget now (deleteLater alone can leave it alive until exit and crash Qt)."""
    widget.setParent(None)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _APP.processEvents()


@pytest.fixture(scope="session")
def qapp():
    yield _APP
    for w in QApplication.topLevelWidgets():
        _dispose(w)
    gc.collect()


@pytest.fixture
def s(tmp_path, qapp):
    folder = tmp_path / "myshop"
    conn, session, recovery = shop.setup_new_shop(folder, "anu", PASSWORD, pin="1234")
    ensure_default_tax_rules(conn)
    product = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(conn, product, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 10, "opening stock")
    view = BackupRestoreView(conn, session, folder)
    # No pop-ups in tests: record what the screen would have shown.
    shown = SimpleNamespace(errors=[], infos=[], confirm_answer=True, confirm_texts=[])
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)

    def confirm(title, text):
        shown.confirm_texts.append(text)
        return shown.confirm_answer

    view._confirm = confirm
    ns = SimpleNamespace(folder=folder, conn=conn, session=session, view=view, shown=shown,
                         variant=variant, recovery=recovery, dest=tmp_path / "usb")
    yield ns
    _dispose(view)
    try:
        conn.close()
    except Exception:
        pass


def _sell(s):
    return create_sale(
        s.conn, [{"variant_id": s.variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
        user_id=s.session.user_id, on_date="2026-11-01",
    )


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- create

def test_create_backup_makes_a_real_encrypted_backup_file(s):
    _sell(s)
    result = s.view.create_backup(s.dest)
    path = Path(result["path"])
    assert path.suffix == backup.EXT and path.exists()
    assert path.read_bytes().startswith(backup.MAGIC)      # our container format, not a raw database copy
    assert backup.verify_backup_file(path)["intact"]
    assert "SUCCESSFUL" in s.view.result.text() and result["file_name"] in s.view.result.text()
    assert s.view.table.rowCount() == 1                    # the new file is listed
    assert _count(s.conn, "audit_log") > 0 and s.conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'backup_created'").fetchone()[0] == 1


def test_create_backup_remembers_the_folder_and_updates_the_reminder(s):
    assert "No backup has been made yet" in s.view.status.text()
    s.view.create_backup(s.dest)
    assert "Last backup" in s.view.status.text()
    again = BackupRestoreView(s.conn, s.session, s.folder)
    assert again.dest == str(s.dest)


def test_failed_backup_is_reported_with_a_reason_and_never_as_success(s, monkeypatch):
    def boom(*a, **k):
        raise backup.BackupError("Not enough free space at the backup location.")
    monkeypatch.setattr(backup, "create_backup", boom)
    assert s.view.create_backup(s.dest) is None
    assert "FAILED" in s.view.result.text() and "SUCCESSFUL" not in s.view.result.text()
    assert s.shown.errors == ["Not enough free space at the backup location."]


def test_unexpected_error_does_not_leak_details(s, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret path C:\\Users\\x\\shop.db password=abc")
    monkeypatch.setattr(backup, "create_backup", boom)
    assert s.view.create_backup(s.dest) is None
    assert "password" not in s.shown.errors[0] and "E-BACKUP" in s.shown.errors[0]


# ---------------------------------------------------------------- test this backup

def test_test_this_backup_reports_counts_and_changes_nothing(s):
    _sell(s)
    result = s.view.create_backup(s.dest)
    _sell(s)
    info = s.view.test_backup(result["path"], "anu", PASSWORD)
    assert info["bill_count"] == 1 and _count(s.conn, "sales") == 2
    assert "Backup test passed" in s.view.result.text()


def test_test_with_wrong_password_fails_cleanly(s):
    result = s.view.create_backup(s.dest)
    assert s.view.test_backup(result["path"], "anu", "wrong-password") is None
    assert "Wrong username, password or recovery key" in s.shown.errors[0]


# ---------------------------------------------------------------- restore

def test_restore_replaces_data_signals_sign_in_and_keeps_a_safety_backup(s):
    _sell(s)
    result = s.view.create_backup(s.dest)
    _sell(s)                                               # work done after the backup
    signals = []
    s.view.restored.connect(lambda: signals.append(True))

    out = s.view.restore_backup(result["path"], "anu", PASSWORD)

    assert out["status"] == "success" and signals == [True]
    assert "REPLACE" in s.shown.confirm_texts[0] and "Bills: 1" in s.shown.confirm_texts[0]
    assert Path(out["safety_backup"]).exists()
    conn, _ = shop.open_shop(s.folder, "anu", PASSWORD)
    assert _count(conn, "sales") == 1
    conn.close()


def test_restore_can_be_cancelled_and_then_changes_nothing(s):
    _sell(s)
    result = s.view.create_backup(s.dest)
    _sell(s)
    s.shown.confirm_answer = False
    signals = []
    s.view.restored.connect(lambda: signals.append(True))
    assert s.view.restore_backup(result["path"], "anu", PASSWORD) is None
    assert signals == [] and _count(s.conn, "sales") == 2


def test_restore_with_wrong_password_changes_nothing(s):
    result = s.view.create_backup(s.dest)
    signals = []
    s.view.restored.connect(lambda: signals.append(True))
    assert s.view.restore_backup(result["path"], "anu", "wrong") is None
    assert signals == [] and s.shown.confirm_texts == []   # never even asked to confirm
    assert _count(s.conn, "users") == 1


def test_restore_of_a_damaged_file_is_refused(s):
    result = s.view.create_backup(s.dest)
    path = Path(result["path"])
    data = bytearray(path.read_bytes())
    data[-10] ^= 0xFF
    path.write_bytes(bytes(data))
    assert s.view.restore_backup(str(path), "anu", PASSWORD) is None
    assert "damaged" in s.shown.errors[0].lower()


# ---------------------------------------------------------------- permissions

def test_cashier_cannot_use_the_buttons(s):
    from core.shop import add_user
    cashier_id = add_user(s.folder, s.conn, s.session, "ravi", "cashier-pass-1", "cashier")
    from core.session import Session
    cashier = Session({"id": cashier_id, "username": "ravi", "role": "cashier"}, db_key=s.session.db_key)
    view = BackupRestoreView(s.conn, cashier, s.folder)
    assert not view.create_btn.isEnabled() and not view.restore_btn.isEnabled()


# ---------------------------------------------------------------- wiring and hygiene

def test_date_format_is_dd_mm_yyyy_24_hour():
    assert pretty_time("2026-11-03 18:30:05") == "03/11/2026 18:30:05"


def test_the_fake_file_copy_backup_is_gone_from_the_dashboard():
    dashboard = (ROOT / "ui" / "dashboard_reports_view.py").read_text(encoding="utf-8")
    engine = (ROOT / "core" / "dashboard_backup_engine.py").read_text(encoding="utf-8")
    assert "create_encrypted_backup" not in dashboard + engine
    assert "shutil.copy" not in engine


def test_backup_menu_opens_the_real_backup_screen(s, qapp):
    from ui.main_window import MainWindow
    profile = {"name": "anu", "role": "Owner"}
    window = MainWindow(s.conn, s.session, profile, folder=s.folder)
    index = window.allowed.index("Backup and Restore")
    assert isinstance(window.pages.widget(index), BackupRestoreView)
    _dispose(window)