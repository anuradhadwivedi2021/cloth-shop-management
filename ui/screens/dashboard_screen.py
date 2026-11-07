"""Dashboard menu: today's figures, best sellers, quick actions (SRS section 5).

This file plugs in by itself (see ui/screens/__init__.py) and replaces the older, smaller
ui/dashboard_page.py. It shows only what core.dashboard_data returns, which comes from the same
tested report functions as the Reports menu, so the figures always agree. A card the user may not
see (no permission) is hidden. No SQL and no money arithmetic here.
"""

import logging
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core import backup
from core.dashboard_data import BEST_PERIODS, dashboard_figures
from core.money import format_rupees
from core.permissions import has_permission
from ui.dashboard_page import MODE_NAMES
from ui.i18n import N_, tr
from ui.widgets import label

MENU_TEXT = "Dashboard"
log = logging.getLogger(__name__)

# (button text, menu entry it opens). A button shows only when the user may open that menu entry.
QUICK_ACTIONS = (
    (N_("New Sale"), "New Sale / Billing"),
    (N_("Add Product"), "Products"),
    (N_("Receive Purchase"), "Purchases"),
    (N_("Search Stock"), "Stock and Variants"),
    (N_("Process Return or Exchange"), "Sales Returns and Exchanges"),
    (N_("Record Expense"), "Expenses"),
    (N_("Record Payment"), "Payments and Cash"),
    (N_("View Daily Report"), "Reports"),
)
PERIOD_TEXT = {
    "today": N_("Today"), "this_week": N_("This week"), "this_month": N_("This month"),
    "last_30_days": N_("Last 30 days"),
}
CASH_STATUS = {"not_opened": N_("Day not opened"), "open": N_("Day open"), "closed": N_("Day closed")}


def _card(caption: str):
    frame = QFrame()
    frame.setObjectName("card")
    col = QVBoxLayout(frame)
    col.setContentsMargins(18, 14, 18, 14)
    col.addWidget(label(tr(caption), "hint", wrap=False))
    value = label("-", "title", wrap=False)
    note = label("", "hint")
    col.addWidget(value)
    col.addWidget(note)
    col.addStretch(1)
    return frame, value, note


def _panel(heading: str):
    frame = QFrame()
    frame.setObjectName("card")
    col = QVBoxLayout(frame)
    col.setContentsMargins(18, 14, 18, 14)
    top = QHBoxLayout()
    top.addWidget(label(tr(heading), "hint", wrap=False))
    top.addStretch(1)
    col.addLayout(top)
    body = label("", None)
    col.addWidget(body)
    col.addStretch(1)
    return frame, top, body


class DashboardScreen(QWidget):
    def __init__(self, conn, session, folder=None):
        super().__init__()
        self.conn, self.session, self.folder = conn, session, folder
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)

        inner = QWidget()
        inner.setObjectName("page")
        inner.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(14)

        head = QHBoxLayout()
        head.addWidget(label(tr("Dashboard"), "title", wrap=False))
        head.addStretch(1)
        self.refresh_btn = QPushButton(tr("Refresh"))
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        outer.addLayout(head)
        self.subtitle = label("", "subtitle", wrap=False)
        outer.addWidget(self.subtitle)
        self.message = label("", "hint")
        outer.addWidget(self.message)

        self.actions = {}
        actions = QGridLayout()
        actions.setSpacing(8)
        for position, (text, menu_text) in enumerate(QUICK_ACTIONS):
            button = QPushButton(tr(text))
            button.clicked.connect(lambda _checked=False, m=menu_text: self._go(m))
            actions.addWidget(button, position // 4, position % 4)
            self.actions[menu_text] = button
        outer.addLayout(actions)

        self.grid = QGridLayout()
        self.grid.setSpacing(14)
        for column in range(3):
            self.grid.setColumnStretch(column, 1)
        self.cards = {}
        specs = (
            ("sales", N_("Today's sales (invoice total)")), ("bills", N_("Bills today")),
            ("returns", N_("Returns and exchanges today")), ("money", N_("Money received today")),
            ("purchases", N_("Purchases received today")), ("stock", N_("Stock in hand (sellable)")),
            ("low", N_("Low-stock items")), ("expenses", N_("Expenses today")),
            ("cash", N_("Cash in hand (expected)")), ("customer_dues", N_("Customers owe you")),
            ("supplier_dues", N_("You owe suppliers")), ("backup", N_("Last backup")),
        )
        for key, caption in specs:
            self.cards[key] = _card(caption)
        self._order = [key for key, _caption in specs]
        # Names the older dashboard page used, kept so existing tests and code keep working.
        self.sales_value, self.sales_note = self.cards["sales"][1:]
        self.bills_value, self.bills_note = self.cards["bills"][1:]
        self.returns_value, self.returns_note = self.cards["returns"][1:]
        self.money_value, self.money_note = self.cards["money"][1:]
        self.stock_value, self.stock_note = self.cards["low"][1:]  # the old "stock" card was the low-stock count
        self.backup_card = self.cards["backup"][0]
        self.backup_value, self.backup_note = self.cards["backup"][1:]
        self.report_cards = [self.cards[k][0] for k in ("sales", "bills", "returns", "money", "low")]
        outer.addLayout(self.grid)

        lists = QHBoxLayout()
        lists.setSpacing(14)
        self.best_panel, best_top, self.best_body = _panel(N_("Best-selling products"))
        self.period = QComboBox()
        for kind in BEST_PERIODS:
            self.period.addItem(tr(PERIOD_TEXT[kind]), kind)
        self.period.setCurrentIndex(BEST_PERIODS.index("this_month"))
        self.period.currentIndexChanged.connect(self.refresh)
        best_top.addWidget(self.period)
        self.idle_panel, _top, self.idle_body = _panel(N_("In stock but not selling"))
        lists.addWidget(self.best_panel, 1)
        lists.addWidget(self.idle_panel, 1)
        outer.addLayout(lists)
        outer.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.addWidget(scroll)
        self.refresh()

    # ------------------------------------------------------------ moving to another menu entry

    def _main_window(self):
        widget = self.parent()
        while widget is not None and not (hasattr(widget, "allowed") and hasattr(widget, "menu")):
            widget = widget.parent()
        return widget

    def _go(self, menu_text: str) -> None:
        window = self._main_window()
        if window is not None and menu_text in window.allowed:
            window.menu.setCurrentRow(window.allowed.index(menu_text))

    def showEvent(self, event) -> None:
        # The first page is shown without a refresh(), and while it was built it had no parent yet, so the
        # quick-action buttons are decided again here, once the page sits inside the main window.
        self._show_quick_actions()
        super().showEvent(event)

    def _show_quick_actions(self) -> None:
        window = self._main_window()
        allowed = window.allowed if window is not None else []
        for menu_text, button in self.actions.items():
            button.setVisible(menu_text in allowed)

    # ------------------------------------------------------------ filling the figures

    def refresh(self) -> None:
        uid = self.session.user_id
        today = date.today()
        self.subtitle.setText(tr("Today: {date}", date=today.strftime("%d/%m/%Y")))
        self._show_quick_actions()
        self.message.setText("")
        self.message.setObjectName("hint")

        figures = dashboard_figures(self.conn, user_id=uid, today=today, best_period=self.period.currentData())
        self._fill_sales(figures)
        self._fill_stock(figures)
        self._fill_money(figures)
        self._fill_lists(figures)
        self._fill_backup(uid)
        self._arrange_cards()

        if figures["errors"]:
            self.message.setText(tr("Some figures could not be loaded. Please tell the owner."))
            self.message.setObjectName("error")
        elif not any(key in figures for key in ("sales", "stock", "cash", "expenses")):
            self.message.setText(tr("Your role does not show sales figures. Use the menu on the left to start work."))
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)

    def _arrange_cards(self) -> None:
        """Put the visible cards next to each other, so a hidden card leaves no gap."""
        for key in self._order:
            self.grid.removeWidget(self.cards[key][0])
        position = 0
        for key in self._order:
            frame = self.cards[key][0]
            if not frame.isHidden():
                self.grid.addWidget(frame, position // 3, position % 3)
                position += 1

    def _set(self, key, value="-", note=""):
        frame, value_label, note_label = self.cards[key]
        frame.setVisible(True)
        value_label.setText(value)
        note_label.setText(note)

    def _hide_unless(self, figures, key, card_keys):
        """Hide the cards in card_keys when the figure `key` is missing (no permission or failed)."""
        if key not in figures:
            for card in card_keys:
                self.cards[card][0].setVisible(False)
            return False
        return True

    def _fill_sales(self, figures) -> None:
        if self._hide_unless(figures, "sales", ("sales", "bills", "returns")):
            s = figures["sales"]
            self._set("sales", format_rupees(s["invoice_total_paise"]),
                      tr("Net sales after returns: {amount} (before tax)", amount=format_rupees(s["net_sales_paise"])))
            self._set("bills", str(s["bill_count"]), tr("{n} item(s) sold", n=s["items_sold"]))
            self._set("returns", str(s["credit_note_count"]),
                      tr("Exchanges: {count}. Refunds: {amount}", count=s["exchange_count"],
                         amount=format_rupees(s["refunds_paise"])))
        if self._hide_unless(figures, "payments", ("money",)):
            modes = figures["payments"]
            self._set(
                "money", format_rupees(sum(m["net_paise"] for m in modes)),
                ", ".join(f"{tr(MODE_NAMES.get(m['mode'], m['mode']))} {format_rupees(m['net_paise'])}" for m in modes)
                or tr("Nothing received yet"),
            )
        if self._hide_unless(figures, "purchases", ("purchases",)):
            p = figures["purchases"]
            self._set("purchases", format_rupees(p["total_paise"]),
                      tr("{n} purchase(s), {qty} item(s)", n=p["count"], qty=p["qty"]))
        if self._hide_unless(figures, "expenses", ("expenses",)):
            self._set("expenses", format_rupees(figures["expenses"]))

    def _fill_stock(self, figures) -> None:
        if self._hide_unless(figures, "stock", ("stock",)):
            s = figures["stock"]
            if s["value_paise"] is None:
                self._set("stock", str(s["total_qty"]))
            else:
                note = tr("Value at cost: {amount}. Method: {method}", amount=format_rupees(s["value_paise"]), method=s["method_label"])
                if s["variants_without_cost"]:
                    note += " " + tr("{n} item(s) have no cost recorded, so they are valued at zero.", n=s["variants_without_cost"])
                self._set("stock", str(s["total_qty"]), note)
        if self._hide_unless(figures, "low_stock", ("low",)):
            low = figures["low_stock"]
            self._set(
                "low", str(len(low)),
                tr("Everything is above its reorder level.") if not low
                else tr("Most urgent: {names}", names=", ".join(f"{r['product']} {r['size']}/{r['colour']}" for r in low[:2])),
            )

    def _fill_money(self, figures) -> None:
        if self._hide_unless(figures, "cash", ("cash",)):
            c = figures["cash"]
            note = tr(CASH_STATUS.get(c["status"], c["status"]))
            self._set("cash", format_rupees(c["expected_paise"]), note)
        credit = figures.get("customer_dues")
        if credit is None:  # credit sales switched off, or not allowed to see
            self.cards["customer_dues"][0].setVisible(False)
        else:
            self._set("customer_dues", format_rupees(credit["total_paise"]),
                      tr("{n} customer(s). Overdue: {amount}", n=credit["customers"], amount=format_rupees(credit["overdue_paise"])))
        if self._hide_unless(figures, "supplier_dues", ("supplier_dues",)):
            d = figures["supplier_dues"]
            self._set("supplier_dues", format_rupees(d["total_paise"]), tr("{n} supplier(s)", n=d["suppliers"]))

    def _fill_lists(self, figures) -> None:
        best = figures.get("best_sellers")
        self.best_panel.setVisible(best is not None)
        if best is not None:
            self.best_body.setText(
                "\n".join(tr("{product}: {qty} sold", product=r["product"], qty=r["net_qty"]) for r in best)
                or tr("No sales in this period.")
            )
        idle = figures.get("no_sales")
        self.idle_panel.setVisible(idle is not None)
        if idle is not None:
            lines = [tr("{product} ({qty} in stock)", product=r["product"], qty=r["qty"]) for r in idle["rows"]]
            if idle["count"] > len(lines):
                lines.append(tr("... and {n} more", n=idle["count"] - len(lines)))
            self.idle_body.setText("\n".join(lines) or tr("Every product in stock has sold in this period."))

    def _fill_backup(self, uid) -> None:
        frame, value, note = self.cards["backup"]
        allowed = has_permission(self.conn, uid, "create_backup")
        frame.setVisible(allowed)
        if not allowed:
            return
        try:
            due = backup.backup_due(self.conn)
        except Exception:
            log.exception("Dashboard: backup status could not be read")
            return
        if due["last_backup_at"] is None:
            value.setText(tr("Never"))
            note.setText(tr("No backup yet. Open Backup and Restore."))
        else:
            value.setText(tr("{days} day(s) ago", days=due["days_since"]))
            text = tr("On {when}.", when=due["last_backup_at"])
            note.setText(text + (" " + tr("Time for a new backup.") if due["due"] else ""))


def build(conn, session, folder):
    return DashboardScreen(conn, session, folder)
