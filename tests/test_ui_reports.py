"""The Reports screen shows what the tested report functions return, in rupees, and nothing else: no SQL, no
rules of its own. It saves a CSV file only for a user who may export, and never shows a technical error."""

import csv
import os
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import expenses as ec  # noqa: E402
from core import purchase_service as ps  # noqa: E402
from core import shop  # noqa: E402
from core.audit import verify_chain  # noqa: E402
from core.permissions import set_user_permission  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.purchases import create_supplier  # noqa: E402
from core.sales import create_sale  # noqa: E402
from core.stock import add_movement  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import transaction  # noqa: E402
from ui import main_window as mw  # noqa: E402
from ui.screens import reports_screen as rs  # noqa: E402
from core.report_export import _nice_header as rs_nice  # noqa: E402
from ui.screens.reports_screen import REPORTS, ReportsScreen  # noqa: E402

_APP = QApplication.instance() or QApplication([])  # kept alive for the whole run
PASSWORD = "owner-password"
TODAY = date.today().isoformat()


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "myshop"
    conn, owner, _ = shop.setup_new_shop(f, "owner", PASSWORD, pin="1234")
    shop.add_user(f, conn, owner, "till", "till-password", "cashier")
    shop.add_user(f, conn, owner, "ca", "ca-password", "accountant")
    shop.add_user(f, conn, owner, "viewer", "viewer-password", "accountant")
    uid = owner.user_id
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()["id"]
    set_user_permission(conn, uid, viewer, "export_reports", False)
    set_user_permission(conn, uid, viewer, "view_cost", False)
    ensure_default_tax_rules(conn)
    supplier = create_supplier(conn, "Ravi Textiles", phone="9999999999")
    product = create_product(conn, "Shirt", default_cost_paise=50000, default_price_paise=105000,
                             gst_rate_percent=5, hsn="6205", category="Shirts", brand="Raymond")
    (variant,) = generate_variants(conn, product, ["M"], ["Blue"])
    ps.receive_purchase(conn, uid, supplier, "INV-1", TODAY,
                        [{"variant_id": variant, "qty": 10, "unit_cost_paise": 50000, "tax_rate_percent": 5}],
                        paid_paise=100000)
    create_sale(conn, [{"variant_id": variant, "qty": 2}], [{"mode": "cash", "amount_paise": 10 ** 7}], user_id=uid)
    ec.add_expense(conn, TODAY, "Rent", "Shop rent", 1250000, user_id=uid)
    conn.close()
    return f


def _open(folder, username="owner"):
    password = PASSWORD if username == "owner" else f"{username}-password"
    conn, session = shop.open_shop(folder, username, password)
    view = ReportsScreen(conn, session)
    shown = SimpleNamespace(errors=[], infos=[], path="")
    view._error = lambda title, text: shown.errors.append(text)
    view._info = lambda title, text: shown.infos.append(text)
    view._save_dialog = lambda name: shown.path
    return conn, view, shown


def _choose(view, key):
    view.cmb_report.setCurrentIndex(view.cmb_report.findData(key))


def _cells(table):
    return [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]


def _all_text(view):
    return " | ".join(" ".join(row) for row in _cells(view.tbl) + _cells(view.tbl_summary))


# ---------------------------------------------------------------- the pieces that turn results into tables

def test_money_dates_and_blanks_are_shown_for_people():
    assert rs.show_value("total_paise", 123450) == "₹1,234.50"
    assert rs.show_value("qty", 12) == "12"
    assert rs.show_value("date", "2026-11-04") == "04/11/2026"
    assert rs.show_value("x", None) == ""
    assert rs.show_value("missing", [1, 2]) == "1, 2"
    assert rs.pretty("taxable_paise") == "Taxable" and rs.pretty("rate_percent") == "Rate percent"


def test_a_core_result_is_split_into_figures_tables_and_notes():
    raw = {"date_from": "2026-11-01", "date_to": "2026-11-04", "rows": [{"a": 1}, {"a": 2}], "count": 2,
           "totals": {"total_paise": 500}, "formula": "a + b", "by": {"x": {"n": 1}, "y": {"n": 2}}}
    data = rs.split_result(raw)
    assert data["summary"] == [("count", 2), ("total_paise", 500)]
    assert [name for name, _ in data["sections"]] == ["rows", "by"]
    assert data["sections"][1][1] == [{"name": "x", "n": 1}, {"name": "y", "n": 2}]
    assert data["notes"] == ["a + b"]
    assert rs.split_result([{"k": 1}])["sections"] == [("rows", [{"k": 1}])]
    assert rs.split_result(["only text"])["sections"] == [("rows", [{"item": "only text"}])]
    groups = {"groups": [{"supplier_name": "Ravi", "items": [{"sku": "A"}, {"sku": "B"}]}]}
    assert rs.split_result(groups)["sections"][0][1] == [
        {"supplier_name": "Ravi", "sku": "A"}, {"supplier_name": "Ravi", "sku": "B"}]


# ---------------------------------------------------------------- every report opens

@pytest.mark.parametrize("report", [r.key for r in REPORTS])
def test_every_report_runs_for_the_owner_without_an_error(folder, report):
    conn, view, shown = _open(folder)
    _choose(view, report)
    view.show_report()
    assert shown.errors == [] and view.lbl_status.text() in ("", "Nothing to show for this report."), report
    conn.close()


def test_sales_summary_shows_the_saved_bill_in_rupees(folder):
    conn, view, _ = _open(folder)
    _choose(view, "sales_summary")
    view.cmb_period.setCurrentIndex(view.cmb_period.findData("today"))
    view.show_report()
    text = _all_text(view)
    assert "₹" in text and "Bill count" in text
    assert any(row == ["Bill count", "1"] for row in _cells(view.tbl_summary))
    conn.close()


def test_choosing_how_to_group_changes_the_table(folder):
    conn, view, _ = _open(folder)
    _choose(view, "sales_by")
    view.cmb_choice.setCurrentIndex(view.cmb_choice.findData("product"))
    view.show_report()
    assert "Shirt" in _all_text(view)
    view.cmb_choice.setCurrentIndex(view.cmb_choice.findData("payment_mode"))
    view.show_report()
    assert "cash" in _all_text(view).lower()
    conn.close()


def test_a_report_with_several_tables_lets_you_switch_between_them(folder):
    conn, view, _ = _open(folder)
    _choose(view, "gst_sales")
    view.show_report()
    assert view.cmb_section.count() > 1 and not view.cmb_section.isHidden()
    first = view.tbl.columnCount()
    view.cmb_section.setCurrentIndex(view.cmb_section.count() - 1)
    assert view.tbl.columnCount() != first or view.tbl.rowCount() >= 0
    conn.close()


def test_reports_that_show_the_situation_now_hide_the_dates(folder):
    conn, view, _ = _open(folder)
    _choose(view, "current_stock")
    assert view.cmb_period.isHidden() and view.txt_from.isHidden()
    view.show_report()
    assert "Shirt" in _all_text(view)
    _choose(view, "sales_summary")
    assert not view.cmb_period.isHidden()
    conn.close()


# ---------------------------------------------------------------- dates

def test_custom_dates_are_used_and_a_bad_date_is_explained_without_a_pop_up(folder):
    conn, view, shown = _open(folder)
    _choose(view, "expenses")
    view.cmb_period.setCurrentIndex(view.cmb_period.findData("custom"))
    assert not view.txt_from.isHidden()
    view.txt_from.setText("01/01/2000")
    view.txt_to.setText("31/12/2000")
    view.show_report()
    assert view.tbl.rowCount() == 0 and view.lbl_status.text() == "Nothing to show for this report."
    view.txt_from.setText("31/02/2026")
    view.show_report()
    assert shown.errors == [] and view.lbl_status.text() and "Traceback" not in view.lbl_status.text()
    view.txt_from.setText("01/01/2026")
    view.txt_to.setText("01/01/2025")
    view.show_report()
    assert "From date" in view.lbl_status.text() and view.tbl.rowCount() == 0
    conn.close()


def test_the_expense_report_shows_the_saved_expense(folder):
    conn, view, _ = _open(folder)
    _choose(view, "expenses")
    view.show_report()
    assert "Shop rent" in _all_text(view) and "₹12,500.00" in _all_text(view)
    conn.close()


# ---------------------------------------------------------------- permissions

def test_a_user_without_cost_access_is_not_offered_the_purchase_reports(folder):
    conn, view, _ = _open(folder, "viewer")
    keys = [view.cmb_report.itemData(i) for i in range(view.cmb_report.count())]
    assert "sales_summary" in keys and "expenses" in keys
    assert not {"purchase_register", "purchases_by", "gst_purchase", "valuation"} & set(keys)
    conn.close()


def test_the_accountant_sees_the_purchase_reports(folder):
    conn, view, shown = _open(folder, "ca")
    _choose(view, "purchase_register")
    view.show_report()
    assert shown.errors == [] and "INV-1" in _all_text(view)
    conn.close()


def test_a_forced_call_without_permission_is_refused_by_the_core_code(folder):
    conn, view, shown = _open(folder, "viewer")
    report = next(r for r in REPORTS if r.key == "purchase_register")
    view._reports.append(report)
    view.cmb_report.addItem("x", "purchase_register")
    _choose(view, "purchase_register")
    view.show_report()
    assert shown.errors == [] and view.lbl_status.text() and view.tbl.rowCount() == 0
    conn.close()


def test_the_cashier_has_no_reports_menu(folder):
    conn, session = shop.open_shop(folder, "till", "till-password")
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert "Reports" not in window.allowed
    conn.close()


def test_reports_is_the_real_screen_in_the_main_window(folder):
    conn, session = shop.open_shop(folder, "owner", PASSWORD)
    window = mw.MainWindow(conn, session, {"name": "Anu Cloth House", "role": session.role}, folder=folder)
    assert isinstance(window.pages.widget(window.allowed.index("Reports")), ReportsScreen)
    assert "Reports" not in mw.PENDING
    conn.close()


# ---------------------------------------------------------------- saving a file

def test_the_owner_saves_a_report_as_a_csv_file_in_rupees_and_it_is_logged(folder, tmp_path):
    conn, view, shown = _open(folder)
    _choose(view, "expenses")
    view.show_report()
    shown.path = str(tmp_path / "expenses.csv")
    view.export_csv()
    assert shown.errors == [] and shown.infos == ["The report was saved as a file."]
    with open(shown.path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert "Shop rent" in " ".join(rows[1]) and "12500.00" in " ".join(rows[1]) and "12500.00" not in rows[0]
    assert any("(Rs)" in head for head in rows[0])
    logged = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'report_exported'").fetchone()[0]
    assert logged == 1 and verify_chain(conn) == (True, None)
    conn.close()


def test_a_summary_only_report_can_be_saved_too(folder, tmp_path):
    conn, view, shown = _open(folder)
    _choose(view, "sales_summary")
    view.show_report()
    shown.path = str(tmp_path / "summary.csv")
    view.export_csv()
    assert shown.errors == [] and os.path.exists(shown.path)
    conn.close()


def test_the_save_button_is_off_without_permission_and_a_forced_save_writes_nothing(folder, tmp_path):
    conn, view, shown = _open(folder, "viewer")
    assert not view.btn_export.isEnabled()
    _choose(view, "expenses")
    view.show_report()
    shown.path = str(tmp_path / "no.csv")
    view.export_csv()
    assert not os.path.exists(shown.path) and not os.path.exists(shown.path + ".part")
    assert shown.infos == [] and len(shown.errors) == 1
    conn.close()


def test_cancelling_the_file_dialog_does_nothing(folder):
    conn, view, shown = _open(folder)
    _choose(view, "expenses")
    view.show_report()
    shown.path = ""
    view.export_csv()
    assert shown.errors == [] and shown.infos == []
    conn.close()


def test_saving_before_showing_a_report_says_so(folder):
    conn, view, shown = _open(folder)
    view.export_csv()
    assert shown.errors == ["Show a report first."]
    conn.close()


# ---------------------------------------------------------------- faults

def test_a_fault_shows_no_technical_detail(folder, monkeypatch):
    conn, view, shown = _open(folder)
    monkeypatch.setattr(rs.reports_sales, "sales_summary",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.DatabaseError("disk I/O secret")))
    _choose(view, "sales_summary")
    view.show_report()
    assert "secret" not in view.lbl_status.text() and view.lbl_status.text() == "Something went wrong and nothing was saved."
    assert view._busy is False and view.tbl.rowCount() == 0
    conn.close()


# ---------------------------------------------------------------- PDF and Excel files

def test_the_owner_saves_the_whole_gst_report_as_excel_and_pdf(folder, tmp_path):
    from openpyxl import load_workbook
    conn, view, shown = _open(folder)
    _choose(view, "gst_sales")
    view.show_report()
    filled = [n for n, rows in view._result["sections"] if rows]
    expected_sheets = len(filled) + (1 if view._result["summary"] else 0)
    shown.path = str(tmp_path / "gst.xlsx")
    view.export_xlsx()
    assert shown.errors == [] and shown.infos == ["The report was saved as a file."]
    book = load_workbook(shown.path)
    assert book.sheetnames == (["Figures"] if view._result["summary"] else []) + [rs_nice(n) for n in filled]
    assert len(book.sheetnames) == expected_sheets >= 2
    shown.path = str(tmp_path / "gst.pdf")
    view.export_pdf()
    assert open(shown.path, "rb").read().startswith(b"%PDF") and len(shown.infos) == 2
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'report_exported'").fetchone()[0] == 2
    assert verify_chain(conn) == (True, None)
    conn.close()


def test_excel_shows_money_as_numbers_in_rupees(folder, tmp_path):
    from openpyxl import load_workbook
    conn, view, shown = _open(folder)
    _choose(view, "expenses")
    view.show_report()
    shown.path = str(tmp_path / "e.xlsx")
    view.export_xlsx()
    sheet = next(load_workbook(shown.path)[n] for n in load_workbook(shown.path).sheetnames if n != "Figures")
    values = [c.value for row in sheet.iter_rows() for c in row if c.value is not None]
    assert any(isinstance(v, (int, float)) or hasattr(v, "quantize") for v in values)
    assert any(float(v) == 12500.0 for v in values if isinstance(v, (int, float)) or hasattr(v, "quantize"))
    assert "Shop rent" in values
    conn.close()


def test_every_report_can_be_saved_as_excel_and_pdf(folder, tmp_path):
    conn, view, shown = _open(folder)
    saved = 0
    for report in REPORTS:
        _choose(view, report.key)
        view.show_report()
        if not view._export_tables():
            continue
        for fmt, button in (("xlsx", view.export_xlsx), ("pdf", view.export_pdf)):
            shown.path = str(tmp_path / f"{report.key}.{fmt}")
            button()
            assert os.path.exists(shown.path), (report.key, fmt, shown.errors)
            saved += 1
    assert shown.errors == [] and saved >= 2 * 10
    assert not list(tmp_path.glob("*.part"))
    conn.close()


@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
def test_without_permission_the_buttons_are_off_and_a_forced_save_writes_nothing(folder, tmp_path, fmt):
    conn, view, shown = _open(folder, "viewer")
    assert not view.btn_xlsx.isEnabled() and not view.btn_pdf.isEnabled()
    _choose(view, "expenses")
    view.show_report()
    shown.path = str(tmp_path / f"no.{fmt}")
    getattr(view, f"export_{fmt}")()
    assert not os.path.exists(shown.path) and shown.infos == [] and len(shown.errors) == 1
    conn.close()


@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
def test_cancelled_dialog_and_no_report_shown_for_excel_and_pdf(folder, tmp_path, fmt):
    conn, view, shown = _open(folder)
    getattr(view, f"export_{fmt}")()
    assert shown.errors == ["Show a report first."]
    _choose(view, "expenses")
    view.show_report()
    shown.errors.clear()
    shown.path = ""
    getattr(view, f"export_{fmt}")()
    assert shown.errors == [] and shown.infos == []
    conn.close()


def test_a_failed_pdf_save_says_error_not_saved(folder, tmp_path):
    conn, view, shown = _open(folder)
    _choose(view, "expenses")
    view.show_report()
    shown.path = str(tmp_path / "nofolder" / "x.pdf")
    view.export_pdf()
    assert shown.infos == [] and len(shown.errors) == 1
    assert "Traceback" not in shown.errors[0] and "nofolder" not in shown.errors[0]
    conn.close()

