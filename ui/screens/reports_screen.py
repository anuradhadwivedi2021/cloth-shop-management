"""Reports menu: pick a report and a period, read it on screen, save it as a PDF, Excel or CSV file.

No SQL and no rules here. Every figure comes from the tested core functions (core.reports_sales,
core.reports_purchase, core.reports_stock, core.reports_money, core.reports_audit); each of them checks the
permission itself, so a hidden button is never the only protection. The file is written by
core.report_export, which needs "export_reports" and writes the export to the audit log.

The screen does not know the columns of each report. It shows what the core function returned: money kept in
paise is shown in rupees, dates as DD/MM/YYYY, and every list of rows becomes a table.
"""

import logging
import re
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core import report_export, reports_audit, reports_money, reports_purchase, reports_sales, reports_stock
from core.money import format_rupees
from core.permissions import has_permission
from core.purchase_service import format_ui_date, parse_ui_date
from core.report_common import PERIODS, resolve_period
from core.shop_settings import get_shop_profile
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Reports"
log = logging.getLogger("clothshop")

PERIOD_TEXT = {
    "today": N_("Today"), "yesterday": N_("Yesterday"), "this_week": N_("This week"),
    "this_month": N_("This month"), "this_fin_year": N_("This financial year"), "custom": N_("Choose dates"),
}
SALES_GROUP_TEXT = {
    "product": N_("Product"), "variant": N_("Size and colour"), "category": N_("Category"), "brand": N_("Brand"),
    "day": N_("Day"), "hour": N_("Hour"), "salesperson": N_("Salesperson"), "payment_mode": N_("Payment method"),
}
PURCHASE_GROUP_TEXT = {
    "supplier": N_("Supplier"), "product": N_("Product"), "variant": N_("Size and colour"),
    "category": N_("Category"), "brand": N_("Brand"), "day": N_("Day"), "month": N_("Month"),
}
STOCK_STATUS_TEXT = {None: N_("All stock"), "low": N_("Low stock"), "out": N_("Out of stock"), "ok": N_("In stock")}

GROUP_TEXT = {"sales": N_("Sales"), "purchases": N_("Purchases"), "stock": N_("Stock"), "money": N_("Money"),
              "audit": N_("Audit and checks")}


class ReportDef:
    """One report: its menu text, the permissions it needs besides 'view_reports', and how to run it.

    run(conn, user_id, start, end, choice) returns whatever the core function returns.
    `choices` (optional): (label text, [(value, text), ...]) for the extra list shown beside the period.
    `needs_dates`: False for reports that show the situation now (stock, audit chain).
    """

    def __init__(self, key, group, title, run, *, needs=(), needs_dates=True, choices=None):
        self.key, self.group, self.title, self.run = key, group, title, run
        self.needs, self.needs_dates, self.choices = tuple(needs), needs_dates, choices


def _choices(label_text, mapping):
    return label_text, list(mapping.items())


REPORTS = [
    ReportDef("sales_summary", "sales", N_("Sales summary"),
              lambda c, u, a, b, x: reports_sales.sales_summary(c, a, b, user_id=u)),
    ReportDef("payments", "sales", N_("Money received, by payment method"),
              lambda c, u, a, b, x: reports_sales.payment_mode_summary(c, a, b, user_id=u)),
    ReportDef("sales_by", "sales", N_("Sales grouped by"),
              lambda c, u, a, b, x: reports_sales.sales_by(c, a, b, x, user_id=u),
              choices=_choices(N_("Group by"), SALES_GROUP_TEXT)),
    ReportDef("day_book", "sales", N_("Day book"),
              lambda c, u, a, b, x: reports_sales.day_book(c, a, b, user_id=u)),
    ReportDef("gst_sales", "sales", N_("GST sales register"),
              lambda c, u, a, b, x: reports_sales.gst_sales_register(c, a, b, user_id=u)),

    ReportDef("purchase_register", "purchases", N_("Purchase register"),
              lambda c, u, a, b, x: reports_purchase.purchase_register(c, a, b, user_id=u), needs=("view_cost",)),
    ReportDef("purchases_by", "purchases", N_("Purchases grouped by"),
              lambda c, u, a, b, x: reports_purchase.purchases_by(c, a, b, x, user_id=u), needs=("view_cost",),
              choices=_choices(N_("Group by"), PURCHASE_GROUP_TEXT)),
    ReportDef("purchase_returns", "purchases", N_("Purchase returns (debit notes)"),
              lambda c, u, a, b, x: reports_purchase.purchase_return_register(c, a, b, user_id=u),
              needs=("view_cost",)),
    ReportDef("gst_purchase", "purchases", N_("GST on purchases"),
              lambda c, u, a, b, x: reports_purchase.gst_purchase_summary(c, a, b, user_id=u), needs=("view_cost",)),

    ReportDef("current_stock", "stock", N_("Current stock"),
              lambda c, u, a, b, x: reports_stock.current_stock(c, user_id=u, status=x), needs_dates=False,
              choices=_choices(N_("Show"), STOCK_STATUS_TEXT)),
    ReportDef("low_stock", "stock", N_("Low stock"),
              lambda c, u, a, b, x: reports_stock.low_stock(c, user_id=u), needs_dates=False),
    ReportDef("reorder", "stock", N_("What to order"),
              lambda c, u, a, b, x: reports_stock.reorder_list(c, user_id=u), needs_dates=False),
    ReportDef("stock_flow", "stock", N_("Stock received and sold"),
              lambda c, u, a, b, x: reports_stock.stock_flow(c, a, b, user_id=u)),
    ReportDef("damaged", "stock", N_("Damaged and waiting-for-check stock"),
              lambda c, u, a, b, x: reports_stock.damaged_stock(c, user_id=u), needs_dates=False),
    ReportDef("valuation", "stock", N_("Stock value at cost"),
              lambda c, u, a, b, x: reports_stock.stock_valuation(c, user_id=u, method=x), needs=("view_cost",),
              needs_dates=False, choices=_choices(N_("Method"), reports_stock.VALUATION_METHODS)),

    ReportDef("cash_book", "money", N_("Cash book"),
              lambda c, u, a, b, x: reports_money.cash_book(c, a, b, user_id=u)),
    ReportDef("expenses", "money", N_("Expense report"),
              lambda c, u, a, b, x: reports_money.expense_report(c, a, b, user_id=u)),

    ReportDef("audit", "audit", N_("Audit log"),
              lambda c, u, a, b, x: reports_audit.audit_report(c, a, b, user_id=u, category=x),
              choices=_choices(N_("Show"), {None: N_("Everything"), **reports_audit.CATEGORY_LABELS})),
    ReportDef("audit_chain", "audit", N_("Audit log tamper check"),
              lambda c, u, a, b, x: reports_audit.check_audit_chain(c, user_id=u), needs_dates=False),
    ReportDef("bill_check", "audit", N_("Check that the reports add up"),
              lambda c, u, a, b, x: reports_sales.reconcile(c, a, b, user_id=u)),
    ReportDef("series_gaps", "audit", N_("Missing bill numbers"),
              lambda c, u, a, b, x: reports_sales.series_gap_check(c, user_id=u), needs_dates=False),
]

_DROP = {"date_from", "date_to", "group_by", "category", "method", "method_label", "category_labels"}
_NOTES = ("note", "formula")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- turning a core result into tables

def pretty(key) -> str:
    """'taxable_paise' -> 'Taxable', 'rate_percent' -> 'Rate percent'."""
    text = str(key)
    if text.endswith("_paise"):
        text = text[:-6]
    text = text.replace("_", " ").strip()
    return (text[:1].upper() + text[1:]) if text else ""


def show_value(key, value) -> str:
    """One cell as text. Money (a *_paise whole number) is shown in rupees."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return tr("Yes") if value else tr("No")
    if str(key).endswith("_paise") and isinstance(value, int):
        return format_rupees(value)
    if isinstance(value, str) and _ISO_DATE.match(value):
        return format_ui_date(value)
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(show_value(key, v) for v in value)
    if isinstance(value, dict):
        text = ", ".join(f"{pretty(k)}: {show_value(k, v)}" for k, v in value.items())
        return text if len(text) <= 160 else text[:157] + "..."
    return str(value)


def _is_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _row_dicts(items) -> list:
    """A list as rows: dicts stay as they are, plain values become one-column rows."""
    return [item if isinstance(item, dict) else {"item": item} for item in items]


def _flatten_groups(groups) -> list:
    """The 'what to order' lists: one row per item, with the supplier name added."""
    rows = []
    for group in groups:
        for item in group.get("items", []):
            rows.append({"supplier_name": group.get("supplier_name"), **item})
    return rows


def split_result(raw) -> dict:
    """Turn what a core report returned into {'summary': [(name, value)], 'sections': [(name, rows)], 'notes': [text]}.

    * single figures (and a 'totals' block of figures) go to the summary;
    * every list of rows becomes a section (a table);
    * a block of blocks (the day book totals) becomes a section with one row per block;
    * 'note' and 'formula' texts are kept to print under the report.
    """
    summary, sections, notes = [], [], []
    if isinstance(raw, list):
        return {"summary": [], "sections": [("rows", _row_dicts(raw))], "notes": []}
    if not isinstance(raw, dict):
        return {"summary": [], "sections": [], "notes": []}
    for key, value in raw.items():
        if key in _DROP:
            continue
        if key in _NOTES and isinstance(value, str):
            notes.append(value)
        elif key == "groups" and isinstance(value, list):
            sections.append(("rows", _flatten_groups(value)))
        elif isinstance(value, list):
            sections.append((key, _row_dicts(value)))
        elif isinstance(value, dict):
            if value and all(isinstance(v, dict) for v in value.values()):
                sections.append((key, [{"name": k, **v} for k, v in value.items()]))
            elif key == "totals" or all(_is_scalar(v) for v in value.values()):
                if key == "totals":
                    summary.extend((k, v) for k, v in value.items() if _is_scalar(v))
                else:
                    sections.append((key, [{"item": k, "value": v} for k, v in value.items()]))
            else:
                summary.append((key, value))
        elif _is_scalar(value):
            summary.append((key, value))
    return {"summary": summary, "sections": sections, "notes": notes}


def columns_of(rows) -> list:
    """Column keys in the order first seen, over all rows."""
    seen = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


# ---------------------------------------------------------------- the screen

class ReportsScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.can_export = has_permission(conn, self.user_id, "export_reports")
        self._reports = [r for r in REPORTS if all(has_permission(conn, self.user_id, p) for p in r.needs)]
        self._result = None      # the last report shown: split_result(...)
        self._current = None     # (ReportDef, start, end) of the last report shown
        self._busy = False

        self.lbl_status = label("", "error")
        self.cmb_report = QComboBox()
        for r in self._reports:
            self.cmb_report.addItem(f"{tr(GROUP_TEXT[r.group])}  –  {tr(r.title)}", r.key)
        self.cmb_period = QComboBox()
        for key in PERIODS:
            self.cmb_period.addItem(tr(PERIOD_TEXT[key]), key)
        self.cmb_period.setCurrentIndex(self.cmb_period.findData("this_month"))
        self.txt_from = QLineEdit(date.today().replace(day=1).strftime("%d/%m/%Y"))
        self.txt_to = QLineEdit(date.today().strftime("%d/%m/%Y"))
        self.lbl_choice = label("", "subtitle", wrap=False)
        self.cmb_choice = QComboBox()
        self.btn_show = QPushButton(tr("Show report"))
        self.btn_export = QPushButton(tr("Save as CSV file…"))
        self.btn_xlsx = QPushButton(tr("Save as Excel file…"))
        self.btn_pdf = QPushButton(tr("Save as PDF file…"))
        self.btn_xlsx.setToolTip(tr("The whole report: the figures and every table."))
        self.btn_pdf.setToolTip(tr("The whole report: the figures and every table."))
        for button in self._export_buttons():
            button.setEnabled(self.can_export)
            if not self.can_export:
                button.setToolTip(tr("You do not have permission to export reports."))
        self.cmb_section = QComboBox()
        self.lbl_notes = label("", "subtitle")
        self.tbl_summary = make_table([tr("Figure"), tr("Value")])
        self.tbl_summary.setMaximumHeight(190)
        self.tbl = make_table([])

        top = QHBoxLayout()
        top.addWidget(label(tr("Report"), "subtitle", wrap=False))
        top.addWidget(self.cmb_report, 1)
        top.addWidget(self.lbl_choice)
        top.addWidget(self.cmb_choice)
        dates = QHBoxLayout()
        self.lbl_period = label(tr("Period"), "subtitle", wrap=False)
        self.lbl_from = label(tr("From"), "subtitle", wrap=False)
        self.lbl_to = label(tr("To"), "subtitle", wrap=False)
        for widget in (self.lbl_period, self.cmb_period, self.lbl_from, self.txt_from, self.lbl_to, self.txt_to):
            dates.addWidget(widget)
        dates.addStretch(1)
        dates.addWidget(self.btn_show)
        for button in self._export_buttons():
            dates.addWidget(button)

        col = QVBoxLayout(self)
        col.setContentsMargins(18, 14, 18, 14)
        col.addWidget(label(tr("Reports"), "title", wrap=False))
        col.addWidget(self.lbl_status)
        col.addLayout(top)
        col.addLayout(dates)
        col.addWidget(self.lbl_notes)
        col.addWidget(self.tbl_summary)
        col.addWidget(self.cmb_section)
        col.addWidget(self.tbl, 1)

        self.cmb_report.currentIndexChanged.connect(self._report_changed)
        self.cmb_period.currentIndexChanged.connect(self._period_changed)
        self.btn_show.clicked.connect(self.show_report)
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_xlsx.clicked.connect(self.export_xlsx)
        self.btn_pdf.clicked.connect(self.export_pdf)
        self.cmb_section.currentIndexChanged.connect(self._fill_section)
        self.txt_from.returnPressed.connect(self.show_report)
        self.txt_to.returnPressed.connect(self.show_report)
        self._report_changed()

    # ------------------------------------------------------------ small helpers that tests replace

    def _export_buttons(self):
        return (self.btn_export, self.btn_xlsx, self.btn_pdf)

    def _save_dialog(self, default_name: str) -> str:
        """The file the user chose to save to, or an empty text when cancelled."""
        kind = {"xlsx": "Excel (*.xlsx)", "pdf": "PDF (*.pdf)"}.get(default_name.rsplit(".", 1)[-1], "CSV (*.csv)")
        path, _filter = QFileDialog.getSaveFileName(self, tr("Save report"), default_name, kind)
        return path or ""

    # ------------------------------------------------------------ choosing

    def _report(self):
        key = self.cmb_report.currentData()
        return next((r for r in self._reports if r.key == key), None)

    def _report_changed(self, *_):
        report = self._report()
        self._clear()
        if report is None:
            self.lbl_status.setText(tr("No report is available for your permissions."))
            self.btn_show.setEnabled(False)
            for button in self._export_buttons():
                button.setEnabled(False)
            return
        self.btn_show.setEnabled(True)
        has_dates = report.needs_dates
        for widget in (self.lbl_period, self.cmb_period):
            widget.setVisible(has_dates)
        self._period_changed()
        self.cmb_choice.blockSignals(True)
        self.cmb_choice.clear()
        if report.choices:
            self.lbl_choice.setText(tr(report.choices[0]))
            for value, text in report.choices[1]:
                self.cmb_choice.addItem(tr(text), value)
        self.cmb_choice.blockSignals(False)
        self.lbl_choice.setVisible(bool(report.choices))
        self.cmb_choice.setVisible(bool(report.choices))

    def _period_changed(self, *_):
        report = self._report()
        custom = report is not None and report.needs_dates and self.cmb_period.currentData() == "custom"
        for widget in (self.lbl_from, self.txt_from, self.lbl_to, self.txt_to):
            widget.setVisible(custom)

    def refresh(self, *_):
        """Called each time the page is opened: show the report again so the figures are current."""
        if self._current is not None:
            self.show_report()

    def _clear(self):
        self._result, self._current = None, None
        self.lbl_notes.setText("")
        self.tbl_summary.setRowCount(0)
        self.tbl_summary.setVisible(False)
        self.cmb_section.blockSignals(True)
        self.cmb_section.clear()
        self.cmb_section.blockSignals(False)
        self.cmb_section.setVisible(False)
        self.tbl.setRowCount(0)
        self.tbl.setColumnCount(0)

    def _dates(self, report):
        if not report.needs_dates:
            return None, None
        kind = self.cmb_period.currentData()
        if kind == "custom":
            return resolve_period(
                "custom", date_from=parse_ui_date(self.txt_from.text(), "From date"),
                date_to=parse_ui_date(self.txt_to.text(), "To date"))
        return resolve_period(kind)

    # ------------------------------------------------------------ showing

    def show_report(self, *_):
        if self._busy:
            return
        report = self._report()
        if report is None:
            return
        self._busy = True
        try:
            def work():
                start, end = self._dates(report)
                choice = self.cmb_choice.currentData() if report.choices else None
                return start, end, report.run(self.conn, self.user_id, start, end, choice)

            ok, result = self._run(work, title=tr("Reports"), quiet_to=self.lbl_status)
        finally:
            self._busy = False
        if not ok:
            self._clear()
            return
        start, end, raw = result
        self._current = (report, start, end)
        self._result = split_result(raw)
        self._display()

    def _display(self):
        data = self._result
        self.lbl_notes.setText("  ".join(data["notes"]))
        self.tbl_summary.setRowCount(0)
        for name, value in data["summary"]:
            add_row(self.tbl_summary, [tr(pretty(name)), show_value(name, value)], right=(1,))
        self.tbl_summary.setVisible(bool(data["summary"]))
        self.cmb_section.blockSignals(True)
        self.cmb_section.clear()
        for name, rows in data["sections"]:
            self.cmb_section.addItem(f"{tr(pretty(name))} ({len(rows)})", name)
        self.cmb_section.blockSignals(False)
        self.cmb_section.setVisible(len(data["sections"]) > 1)
        self._fill_section()

    def _section_rows(self) -> list:
        index = self.cmb_section.currentIndex()
        sections = self._result["sections"] if self._result else []
        return sections[index][1] if 0 <= index < len(sections) else []

    def _fill_section(self, *_):
        rows = self._section_rows()
        keys = columns_of(rows)
        self.tbl.setRowCount(0)
        self.tbl.setColumnCount(len(keys))
        self.tbl.setHorizontalHeaderLabels([tr(pretty(k)) for k in keys])
        money = {i for i, k in enumerate(keys) if str(k).endswith("_paise") or str(k).endswith("_qty")}
        for row in rows:
            add_row(self.tbl, [show_value(k, row.get(k)) for k in keys], right=money)
        has_tables = bool(self._result and self._result["sections"])
        has_figures = bool(self._result and self._result["summary"])
        self.lbl_status.setText("" if (rows or (has_figures and not has_tables)) else tr("Nothing to show for this report."))

    # ------------------------------------------------------------ saving a file

    def _export_rows(self) -> list:
        rows = self._section_rows()
        if rows:
            return rows
        return [{"figure": pretty(n), "value": show_value(n, v)} for n, v in self._result["summary"]]

    def export_csv(self, *_):
        if self._busy:
            return
        if self._current is None or self._result is None:
            self._error(tr("Reports"), tr("Show a report first."))
            return
        report, start, end = self._current
        rows = self._export_rows()
        if not rows:
            self._error(tr("Reports"), tr("There is nothing to export for this report."))
            return
        stamp = f"_{start}_{end}" if start else f"_{date.today().isoformat()}"
        path = self._save_dialog(f"{report.key}{stamp}.csv")
        if not path:
            return
        self._busy = True
        try:
            ok, _ = self._run(lambda: report_export.export_report_csv(
                self.conn, path, rows, user_id=self.user_id, report_name=report.key, columns=columns_of(rows)),
                title=tr("Reports"))
        finally:
            self._busy = False
        if ok:
            self._info(tr("Reports"), tr("The report was saved as a file."))


    def _export_tables(self) -> list:
        """The whole report for a PDF / Excel file: the figures, then every table."""
        tables = []
        if self._result["summary"]:
            tables.append(("figures", [{"figure": pretty(n), "value": show_value(n, v)}
                                       for n, v in self._result["summary"]]))
        tables.extend((name, rows) for name, rows in self._result["sections"] if rows)
        return tables

    def _export_file(self, fmt: str) -> None:
        if self._busy:
            return
        if self._current is None or self._result is None:
            self._error(tr("Reports"), tr("Show a report first."))
            return
        report, start, end = self._current
        tables = self._export_tables()
        if not tables:
            self._error(tr("Reports"), tr("There is nothing to export for this report."))
            return
        stamp = f"_{start}_{end}" if start else f"_{date.today().isoformat()}"
        path = self._save_dialog(f"{report.key}{stamp}.{fmt}")
        if not path:
            return
        subtitle = f"{format_ui_date(start)} - {format_ui_date(end)}" if start else ""
        shop_name = ""
        try:
            shop_name = get_shop_profile(self.conn).get("name") or ""
        except Exception:  # noqa: BLE001 - the name is only decoration on the PDF footer
            log.exception("Shop name unavailable for the report footer")
        self._busy = True
        try:
            ok, _ = self._run(lambda: report_export.export_report_file(
                self.conn, path, tables, fmt=fmt, user_id=self.user_id, report_name=report.title,
                subtitle=subtitle, notes=self._result["notes"], shop_name=shop_name), title=tr("Reports"))
        finally:
            self._busy = False
        if ok:
            self._info(tr("Reports"), tr("The report was saved as a file."))

    def export_xlsx(self, *_):
        self._export_file("xlsx")

    def export_pdf(self, *_):
        self._export_file("pdf")


def build(conn, session, folder):
    return ReportsScreen(conn, session)