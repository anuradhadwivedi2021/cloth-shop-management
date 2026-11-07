import csv
from types import SimpleNamespace

import pytest

from core import auth
from core.permissions import PermissionDenied
from core.report_export import MAX_PDF_ROWS, export_report_csv, export_report_file
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


# ====================================================================== Excel and PDF

TABLES = [
    ("figures", [{"figure": "Bills", "value": "3"}]),
    ("rows", [{"party": "Ravi", "date": "2026-11-06", "qty": 3, "total_paise": 105050, "note": None},
              {"party": "=HYPERLINK(\"x\")", "date": "2026-11-07", "qty": -1, "total_paise": -250, "note": "ok"}]),
    ("empty table", []),
]


def _xlsx(shop, name="r", **kw):
    return export_report_file(shop.conn, shop.dir / name, kw.pop("tables", TABLES), fmt="xlsx", user_id=shop.owner,
                              report_name=kw.pop("report_name", "Day book"), **kw)


def test_excel_keeps_money_as_exact_numbers_and_readable_headers(shop):
    from openpyxl import load_workbook
    out = _xlsx(shop, subtitle="01/11/2026 - 07/11/2026", notes=["a + b"])
    assert out.endswith("r.xlsx")
    book = load_workbook(out)
    assert book.sheetnames == ["Figures", "Rows"]                   # the empty table gets no sheet
    sheet = book["Rows"]
    header = [c.value for c in sheet[4]]
    assert header == ["Party", "Date", "Qty", "Total (Rs)", "Note"]
    first, second = [[c.value for c in sheet[r]] for r in (5, 6)]
    assert first[0] == "Ravi" and first[1] == "06/11/2026" and first[2] == 3 and float(first[3]) == 1050.5
    assert first[4] is None
    assert second[0] == "'=HYPERLINK(\"x\")" and float(second[3]) == -2.5          # formula guarded, negative exact
    assert sheet.cell(row=5, column=4).number_format == "#,##0.00"
    assert sheet["A1"].value == "Day book" and sheet["A2"].value == "01/11/2026 - 07/11/2026"
    assert any(c.value == "a + b" for row in sheet.iter_rows() for c in row)    # notes sit under the last sheet


def test_pdf_is_written_for_normal_wide_hindi_and_odd_values(shop):
    wide = [{f"c{i}_paise" if i % 2 else f"c{i}": 12345 * i for i in range(14)}]
    hindi = [{"name": "कमीज़ <b>&", "total_paise": 100, "x": {"a": 1}, "flag": True, "ratio": 0.5}]
    out = export_report_file(shop.conn, shop.dir / "r", [("rows", TABLES[1][1]), ("wide", wide), ("hindi", hindi)],
                             fmt="pdf", user_id=shop.owner, report_name="Day book", subtitle="x", notes=["n <i>"],
                             shop_name="My Shop")
    data = open(out, "rb").read()
    assert out.endswith("r.pdf") and data.startswith(b"%PDF") and len(data) > 2000


def test_pdf_refuses_a_huge_report_and_leaves_nothing_behind(shop):
    rows = [{"n": i} for i in range(MAX_PDF_ROWS + 1)]
    with pytest.raises(ValueError, match="Excel or CSV"):
        export_report_file(shop.conn, shop.dir / "big", [("rows", rows)], fmt="pdf", user_id=shop.owner,
                           report_name="t")
    assert list(shop.dir.glob("big*")) == []
    assert shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'report_exported'").fetchone()[0] == 0


@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
def test_cashier_cannot_export_excel_or_pdf_and_no_file_is_created(shop, fmt):
    with pytest.raises(PermissionDenied):
        export_report_file(shop.conn, shop.dir / "r", TABLES, fmt=fmt, user_id=shop.till, report_name="t")
    assert list(shop.dir.glob("r*")) == []


@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
def test_excel_and_pdf_exports_are_audited_with_their_format(shop, fmt):
    export_report_file(shop.conn, shop.dir / "r", TABLES, fmt=fmt, user_id=shop.owner, report_name="Day book")
    row = shop.conn.execute("SELECT user_id, after_json FROM audit_log WHERE action = 'report_exported'").fetchone()
    assert row[0] == shop.owner and fmt in row[1] and "Day book" in row[1] and "r." + fmt in row[1]


@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
def test_nothing_to_export_is_refused(shop, fmt):
    with pytest.raises(ValueError, match="nothing to export"):
        export_report_file(shop.conn, shop.dir / "r", [("a", []), ("b", [])], fmt=fmt, user_id=shop.owner,
                           report_name="t")
    assert list(shop.dir.glob("r*")) == []


def test_unknown_format_is_refused(shop):
    with pytest.raises(ValueError, match="xlsx or pdf"):
        export_report_file(shop.conn, shop.dir / "r", TABLES, fmt="docx", user_id=shop.owner, report_name="t")


@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
def test_failed_save_leaves_no_half_file_and_no_audit_entry(shop, monkeypatch, fmt):
    import core.report_export as mod
    monkeypatch.setattr(mod.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        export_report_file(shop.conn, shop.dir / "r", TABLES, fmt=fmt, user_id=shop.owner, report_name="t")
    assert list(shop.dir.glob("r*")) == []
    assert shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'report_exported'").fetchone()[0] == 0


def test_a_missing_folder_leaves_no_part_file_and_the_extension_is_not_doubled(shop):
    with pytest.raises(OSError):
        _xlsx(shop, name="nofolder/x")
    assert _xlsx(shop, name="named.xlsx").endswith("named.xlsx")
    assert [p.name for p in shop.dir.glob("named*")] == ["named.xlsx"]


def test_sheet_names_are_legal_and_unique(shop):
    from openpyxl import load_workbook
    tables = [("a/b:c", [{"k": 1}]), ("a/b:c", [{"k": 2}]), ("x" * 60, [{"k": 3}])]
    book = load_workbook(_xlsx(shop, tables=tables))
    assert len({n.lower() for n in book.sheetnames}) == 3 and all(len(n) <= 31 for n in book.sheetnames)

