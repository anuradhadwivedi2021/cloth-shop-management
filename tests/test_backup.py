import os
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import backup, shop
from core.audit import verify_chain
from core.backup import BackupError
from core.permissions import PermissionDenied
from core.products import create_product, generate_variants
from core.sales import create_sale
from core.settings import get_setting
from core.stock import add_movement, get_stock
from core.tax import ensure_default_tax_rules
from db.connection import transaction

DAY = "2026-11-01"
PASSWORD = "owner-password"


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _sell(s, qty=1):
    return create_sale(
        s.conn, [{"variant_id": s.variant, "qty": qty}],
        [{"mode": "cash", "amount_paise": 105000 * qty}],
        user_id=s.session.user_id, on_date=DAY,
    )


@pytest.fixture
def s(tmp_path):
    folder = tmp_path / "myshop"
    conn, session, recovery = shop.setup_new_shop(folder, "anu", PASSWORD, pin="1234")
    ensure_default_tax_rules(conn)
    product = create_product(conn, "Zebra Print Shirt", hsn="6205", default_price_paise=105000)
    (variant,) = generate_variants(conn, product, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, variant, 10, "opening stock")
    ns = SimpleNamespace(
        folder=folder, conn=conn, session=session, recovery=recovery,
        variant=variant, dest=tmp_path / "usb",
    )
    _sell(ns)  # one saved bill
    yield ns
    try:
        ns.conn.close()
    except Exception:
        pass


def _backup(s, **kw):
    return backup.create_backup(s.conn, s.session, s.folder, s.dest, **kw)


# ---------------------------------------------------------------- create

def test_create_backup_makes_one_encrypted_file(s):
    result = _backup(s, now=datetime(2026, 11, 1, 18, 30, 5))
    path = Path(result["path"])
    assert result["status"] == "success"
    assert result["created_at"] == "2026-11-01 18:30:05"
    assert path.name == "ClothShop_backup_20261101_183005.csbak"
    assert path.exists() and result["size_bytes"] == path.stat().st_size
    assert result["bill_count"] == 1
    raw = path.read_bytes()
    assert b"SQLite format" not in raw
    assert b"Zebra" not in raw and b"INV/" not in raw  # nothing readable inside
    assert list(s.dest.iterdir()) == [path]  # no temp files left behind


def test_backup_records_time_and_status_and_audit(s):
    _backup(s, now=datetime(2026, 11, 1, 18, 30, 5))
    assert get_setting(s.conn, "last_backup_at") == "2026-11-01 18:30:05"
    assert get_setting(s.conn, "last_backup_status") == "success"
    row = s.conn.execute("SELECT user_id FROM audit_log WHERE action = 'backup_created'").fetchone()
    assert row[0] == s.session.user_id
    assert verify_chain(s.conn) == (True, None)


def test_warns_when_backup_is_on_the_same_disk_as_the_shop(s):
    assert _backup(s)["same_disk_warning"] is True  # both are inside the same temp disk


def test_two_backups_in_the_same_second_do_not_overwrite(s):
    t = datetime(2026, 11, 1, 18, 30, 5)
    a, b = _backup(s, now=t), _backup(s, now=t)
    assert a["path"] != b["path"] and len(list(s.dest.iterdir())) == 2


def test_failed_backup_leaves_nothing_and_says_so(s):
    with pytest.raises(BackupError, match="free space"):
        _backup(s, min_free_bytes=10 ** 18)
    assert not list(s.dest.iterdir())
    assert get_setting(s.conn, "last_backup_status") == "failed"
    assert get_setting(s.conn, "last_backup_at") is None


def test_crash_while_saving_leaves_no_half_backup(s, monkeypatch):
    def boom(src, dst):
        raise OSError("disk unplugged")

    monkeypatch.setattr(backup.os, "replace", boom)
    with pytest.raises(BackupError, match="could not be created"):
        _backup(s)
    assert not list(s.dest.iterdir())


def test_only_users_with_permission_can_back_up(s):
    shop.add_user(s.folder, s.conn, s.session, "till", "till-password", "cashier")
    s.conn.close()
    conn, till = shop.open_shop(s.folder, "till", "till-password")
    with pytest.raises(PermissionDenied):
        backup.create_backup(conn, till, s.folder, s.dest)
    conn.close()
    assert not s.dest.exists() or not list(s.dest.iterdir())


# ---------------------------------------------------------------- test / preview

def test_preview_shows_date_bill_count_and_last_bill_number(s):
    result = _backup(s, now=datetime(2026, 11, 1, 18, 30, 5))
    info = backup.inspect_backup(result["path"], "anu", PASSWORD)
    assert info["created_at"] == "2026-11-01 18:30:05"
    assert info["bill_count"] == 1
    assert info["last_bill_no"] == "INV/26-27/000001"
    assert info["counts"]["products"] == 1 and info["counts"]["variants"] == 1
    assert info["sales_total_paise"] == 105000
    assert info["newer_than_this_app"] is False


def test_preview_changes_nothing(s):
    result = _backup(s)
    before = _count(s.conn, "audit_log")
    backup.inspect_backup(result["path"], "anu", PASSWORD)
    assert _count(s.conn, "audit_log") == before


def test_backup_opens_with_the_recovery_key_too(s):
    result = _backup(s)
    assert backup.inspect_backup(result["path"], "recovery", s.recovery)["bill_count"] == 1


def test_wrong_password_is_refused(s):
    result = _backup(s)
    with pytest.raises(BackupError, match="Wrong username, password or recovery key"):
        backup.inspect_backup(result["path"], "anu", "not-the-password")


def test_damaged_file_is_detected_without_a_password(s):
    path = Path(_backup(s)["path"])
    assert backup.verify_backup_file(path)["intact"] is True
    data = bytearray(path.read_bytes())
    data[-5] ^= 0xFF  # flip one byte near the end
    path.write_bytes(bytes(data))
    with pytest.raises(BackupError, match="damaged"):
        backup.verify_backup_file(path)
    with pytest.raises(BackupError, match="damaged"):
        backup.inspect_backup(path, "anu", PASSWORD)


def test_truncated_and_foreign_files_are_refused(s):
    path = Path(_backup(s)["path"])
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(BackupError, match="damaged"):
        backup.verify_backup_file(path)
    other = s.dest / "notes.csbak"
    other.write_text("hello")
    with pytest.raises(BackupError, match="not a Cloth Shop backup"):
        backup.verify_backup_file(other)


def test_backup_from_a_newer_app_version_is_flagged_and_refused(s, monkeypatch):
    result = _backup(s)
    monkeypatch.setattr(backup, "MIGRATIONS", {1: ""})
    assert backup.inspect_backup(result["path"], "anu", PASSWORD)["newer_than_this_app"] is True
    with pytest.raises(BackupError, match="newer version"):
        backup.restore_backup(s.folder, result["path"], "anu", PASSWORD, conn=s.conn, session=s.session, confirm=True)


# ---------------------------------------------------------------- restore

def test_restore_on_a_clean_machine(s, tmp_path):
    result = _backup(s)
    clean = tmp_path / "new-laptop"
    out = backup.restore_backup(clean, result["path"], "anu", PASSWORD, confirm=True)
    assert out["status"] == "success" and out["bill_count"] == 1
    assert out["last_bill_no"] == "INV/26-27/000001"
    assert out["safety_backup"] is None
    conn, session = shop.open_shop(clean, "anu", PASSWORD)
    assert _count(conn, "products") == 1 and _count(conn, "sales") == 1
    assert get_stock(conn, s.variant) == 9  # 10 received, 1 sold
    assert verify_chain(conn) == (True, None)
    conn.close()


def test_restore_on_a_clean_machine_with_the_recovery_key(s, tmp_path):
    result = _backup(s)
    clean = tmp_path / "new-laptop"
    backup.restore_backup(clean, result["path"], "recovery", s.recovery, confirm=True)
    conn, _ = shop.open_shop(clean, "anu", PASSWORD)
    assert _count(conn, "sales") == 1
    conn.close()


def test_restore_needs_confirmation(s, tmp_path):
    result = _backup(s)
    with pytest.raises(BackupError, match="not confirmed"):
        backup.restore_backup(tmp_path / "x", result["path"], "anu", PASSWORD)
    assert not (tmp_path / "x").exists()


def test_restore_over_existing_shop_replaces_data_and_keeps_a_safety_backup(s):
    result = _backup(s)
    _sell(s, qty=2)  # work done AFTER the backup
    assert _count(s.conn, "sales") == 2
    out = backup.restore_backup(
        s.folder, result["path"], "anu", PASSWORD, conn=s.conn, session=s.session, confirm=True
    )
    safety = Path(out["safety_backup"])
    assert safety.exists() and safety.parent.name == backup.SAFETY_FOLDER

    conn, _ = shop.open_shop(s.folder, "anu", PASSWORD)
    assert _count(conn, "sales") == 1 and get_stock(conn, s.variant) == 9
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'backup_restored'").fetchone()[0] == 1
    conn.close()

    # the safety backup still holds the newer work, so nothing was silently destroyed
    assert backup.inspect_backup(safety, "anu", PASSWORD)["bill_count"] == 2


def test_restoring_an_older_backup_never_reuses_a_bill_number(s):
    result = _backup(s)  # holds INV/000001
    _sell(s)             # 000002
    _sell(s)             # 000003
    used = {r[0] for r in s.conn.execute("SELECT invoice_no FROM sales")}
    backup.restore_backup(s.folder, result["path"], "anu", PASSWORD, conn=s.conn, session=s.session, confirm=True)

    conn, session = shop.open_shop(s.folder, "anu", PASSWORD)
    s.conn, s.session = conn, session
    new_id = _sell(s)
    new_no = conn.execute("SELECT invoice_no FROM sales WHERE id = ?", (new_id,)).fetchone()[0]
    assert new_no not in used
    assert new_no == "INV/26-27/000004"
    assert conn.execute("SELECT COUNT(*) FROM sales WHERE invoice_no = ?", (new_no,)).fetchone()[0] == 1


def test_wrong_password_restore_changes_nothing(s):
    result = _backup(s)
    _sell(s)
    with pytest.raises(BackupError, match="Wrong username"):
        backup.restore_backup(s.folder, result["path"], "anu", "nope-nope", conn=s.conn, session=s.session, confirm=True)
    assert _count(s.conn, "sales") == 2  # connection still open, data untouched
    assert not (s.folder / backup.SAFETY_FOLDER).exists()


def test_damaged_backup_restore_changes_nothing(s):
    path = Path(_backup(s)["path"])
    data = bytearray(path.read_bytes())
    data[-5] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(BackupError, match="damaged"):
        backup.restore_backup(s.folder, path, "anu", PASSWORD, conn=s.conn, session=s.session, confirm=True)
    assert _count(s.conn, "sales") == 1


def test_restoring_over_an_existing_shop_needs_sign_in_and_permission(s):
    result = _backup(s)
    with pytest.raises(BackupError, match="Sign in"):
        backup.restore_backup(s.folder, result["path"], "anu", PASSWORD, confirm=True)

    shop.add_user(s.folder, s.conn, s.session, "till", "till-password", "cashier")
    s.conn.close()
    conn, till = shop.open_shop(s.folder, "till", "till-password")
    with pytest.raises(PermissionDenied):
        backup.restore_backup(s.folder, result["path"], "anu", PASSWORD, conn=conn, session=till, confirm=True)
    assert _count(conn, "sales") == 1
    conn.close()


def test_failed_swap_keeps_the_current_shop(s, monkeypatch):
    result = _backup(s)
    real_replace = os.replace

    def flaky(src, dst):
        if str(dst).endswith("shop.db"):
            raise OSError("disk error")
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", flaky)
    with pytest.raises(BackupError, match="not replaced"):
        backup.restore_backup(s.folder, result["path"], "anu", PASSWORD, conn=s.conn, session=s.session, confirm=True)
    assert not [p for p in s.folder.iterdir() if p.name.startswith(".tmp-restore")]


# ---------------------------------------------------------------- reminder and pruning

def test_backup_reminder(s):
    assert backup.backup_due(s.conn, today=date(2026, 11, 1))["due"] is True  # never backed up
    _backup(s, now=datetime(2026, 11, 1, 10, 0, 0))
    assert backup.backup_due(s.conn, today=date(2026, 11, 1))["due"] is False
    assert backup.backup_due(s.conn, today=date(2026, 11, 7))["due"] is False
    late = backup.backup_due(s.conn, today=date(2026, 11, 8))
    assert late["due"] is True and late["days_since"] == 7


def test_prune_keeps_the_newest_and_touches_only_backups(s):
    for day in (1, 2, 3, 4):
        _backup(s, now=datetime(2026, 11, day, 9, 0, 0))
    (s.dest / "my-photos.zip").write_text("keep me")
    deleted = backup.prune_backups(s.conn, s.session, s.dest, keep=2)
    assert len(deleted) == 2
    left = sorted(p.name for p in s.dest.iterdir())
    assert left == [
        "ClothShop_backup_20261103_090000.csbak",
        "ClothShop_backup_20261104_090000.csbak",
        "my-photos.zip",
    ]
    with pytest.raises(BackupError, match="at least 1"):
        backup.prune_backups(s.conn, s.session, s.dest, keep=0)


def test_list_backups_marks_damaged_files(s):
    good = Path(_backup(s, now=datetime(2026, 11, 1, 9, 0, 0))["path"])
    bad = s.dest / "ClothShop_backup_20261102_090000.csbak"
    bad.write_bytes(b"garbage")
    listed = backup.list_backups(s.dest)
    assert [b["file_name"] for b in listed] == [bad.name, good.name]
    assert [b["intact"] for b in listed] == [False, True]
