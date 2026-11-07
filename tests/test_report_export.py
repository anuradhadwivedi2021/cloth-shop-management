import csv
from types import SimpleNamespace

import pytest

from core import auth
from core.permissions import PermissionDenied
from core.report_export import export_report_csv
from db.connection import open_db


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    owner = auth.setup_first_owner(conn, "anu", "owner-password", full_name="Anuradha")
    till = auth.create_user(conn, owner, "till", "till-password", "cashier")
    yield SimpleNamespace(conn=conn, owner=owner, till=till, dir=tmp_path)
    conn.close()


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def test_paise_become_rupees_and_header_is_renamed(shop):
    rows = [{"number": "INV/26-27/000001", "total_paise": 123450}, {"number": "INV/26-27/000002", "total_paise": -5}]
    out = export_report_csv(shop.conn, shop.dir / "r.csv", {"rows": rows}, user_id=shop.owner, report_name="t")
    assert _read(out) == [["number", "total (Rs)"], ["INV/26-27/000001", "1234.50"], ["INV/26-27/000002", "-0.05"]]


def test_formula_text_is_neutralised_but_numbers_are_not(shop):
    rows = [{"party": "=HYPERLINK(\"x\")", "note": "-ok", "qty": -3}]
    out = export_report_csv(shop.conn, shop.dir / "r.csv", rows, user_id=shop.owner, report_name="t")
    assert _read(out)[1] == ["'=HYPERLINK(\"x\")", "'-ok", "-3"]


def test_hindi_text_and_chosen_columns(shop):
    rows = [{"name": "कमीज़", "size": "M", "skip": 1}]
    out = export_report_csv(shop.conn, shop.dir / "r.csv", rows, user_id=shop.owner, report_name="t",
                            columns=["size", "name"])
    assert _read(out) == [["size", "name"], ["M", "कमीज़"]]


def test_cashier_cannot_export_and_no_file_is_created(shop):
    with pytest.raises(PermissionDenied):
        export_report_csv(shop.conn, shop.dir / "r.csv", [{"a": 1}], user_id=shop.till, report_name="t")
    assert not (shop.dir / "r.csv").exists()


def test_export_is_written_to_the_audit_log(shop):
    export_report_csv(shop.conn, shop.dir / "r.csv", [{"a": 1}], user_id=shop.owner, report_name="Day book")
    row = shop.conn.execute("SELECT user_id, entity FROM audit_log WHERE action = 'report_exported'").fetchone()
    assert (row[0], row[1]) == (shop.owner, "report")


def test_nothing_to_export_is_refused_and_no_part_file_is_left(shop):
    with pytest.raises(ValueError):
        export_report_csv(shop.conn, shop.dir / "r.csv", {"rows": []}, user_id=shop.owner, report_name="t")
    assert list(shop.dir.glob("r.csv*")) == []


def test_failed_save_leaves_no_half_file(shop, monkeypatch):
    import core.report_export as mod
    monkeypatch.setattr(mod.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        export_report_csv(shop.conn, shop.dir / "r.csv", [{"a": 1}], user_id=shop.owner, report_name="t")
    assert list(shop.dir.glob("r.csv*")) == []
    assert shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'report_exported'").fetchone()[0] == 0


def test_day_book_shape_with_entries_is_exported(shop):
    book = {"entries": [{"date": "2026-11-06", "type": "sale", "total_paise": 105000}], "totals": {}}
    out = export_report_csv(shop.conn, shop.dir / "r.csv", book, user_id=shop.owner, report_name="Day book")
    assert _read(out) == [["date", "type", "total (Rs)"], ["2026-11-06", "sale", "1050.00"]]