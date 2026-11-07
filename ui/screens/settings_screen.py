"""Settings menu: shop details, preferences, GST rates and document numbers.

No SQL and no rules here. core.shop_settings checks and saves preferences (all or nothing, audit-logged);
core.tax_admin adds or ends GST rate rules; core.series_admin changes a document number prefix.
A message says "saved" only after the core call really returned.
"""

import logging

from PySide6.QtCore import Qt
from datetime import date

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QInputDialog, QLineEdit, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from core import series_admin, tax_admin
from core import shop_settings as cfg
from core.money import format_rupees, parse_rupees
from core.purchase_service import format_ui_date, parse_ui_date
from ui.i18n import N_, SUPPORTED, tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import form, label

MENU_TEXT = "Settings"
log = logging.getLogger("clothshop")

SCHEME_TEXT = {
    "regular": N_("Regular GST (tax invoice)"),
    "composition": N_("Composition scheme (bill of supply)"),
    "unregistered": N_("Not registered (simple bill)"),
}
CHOICE_TEXT = {
    **SCHEME_TEXT,
    "a4": N_("A4"), "a5": N_("A5"), "thermal58": N_("Thermal 58 mm"), "thermal80": N_("Thermal 80 mm"),
}
RULE_STATUS = {"active": N_("Active"), "future": N_("Starts later"), "ended": N_("Ended")}
# Preferences that are read when someone signs in, so a change shows from the next sign-in.
NEXT_SIGN_IN = {"ui_language", "auto_lock_minutes"}


class RuleDialog(QDialog):
    """Asks for the details of a new GST rate rule. values() returns what was typed (as text)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("New GST rate rule"))
        self.txt_hsn, self.txt_rate, self.txt_limit = QLineEdit(), QLineEdit(), QLineEdit()
        self.txt_from, self.txt_note = QLineEdit(date.today().strftime("%d/%m/%Y")), QLineEdit()
        self.txt_hsn.setPlaceholderText(tr("For example 62"))
        self.txt_limit.setPlaceholderText(tr("Empty = no price limit"))
        rows = form()
        rows.addRow(tr("HSN starts with"), self.txt_hsn)
        rows.addRow(tr("GST %"), self.txt_rate)
        rows.addRow(tr("Price of one piece up to (₹)"), self.txt_limit)
        rows.addRow(tr("Starts on (DD/MM/YYYY)"), self.txt_from)
        rows.addRow(tr("Note"), self.txt_note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        col = QVBoxLayout(self)
        col.addLayout(rows)
        col.addWidget(buttons)

    def values(self) -> dict:
        return {"hsn": self.txt_hsn.text(), "rate": self.txt_rate.text(), "limit": self.txt_limit.text(),
                "start": self.txt_from.text(), "note": self.txt_note.text()}


class SettingsScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._profile = {}       # the shop details as loaded (to see what the owner changed)
        self._pref_widgets = {}  # preference key -> (kind, widget)
        self._rules = []         # rows of the GST rate table
        self._series = []        # rows of the document number table
        self._dirty = False
        self._busy = False       # blocks a second click while a save runs

        self.lbl_status = label("", "error")
        self.lbl_dirty = label("", "subtitle")
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_shop_tab(), tr("Shop details"))
        self.tabs.addTab(self._build_prefs_tab(), tr("Preferences"))
        self.tabs.addTab(self._build_tax_tab(), tr("GST rates"))
        self.tabs.addTab(self._build_series_tab(), tr("Document numbers"))

        col = QVBoxLayout(self)
        col.setContentsMargins(18, 14, 18, 14)
        col.addWidget(label(tr("Settings"), "title", wrap=False))
        col.addWidget(self.lbl_status)
        col.addWidget(self.lbl_dirty)
        col.addWidget(self.tabs, 1)
        self.refresh()

    # ------------------------------------------------------------ layout

    def _build_shop_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        box = QGroupBox(tr("Shop details"))
        rows = form()
        self.txt_name, self.txt_address, self.txt_phone = QLineEdit(), QLineEdit(), QLineEdit()
        self.txt_email, self.txt_gstin, self.txt_upi = QLineEdit(), QLineEdit(), QLineEdit()
        self.cmb_state = QComboBox()
        self.cmb_state.addItem(tr("Not set"), None)
        for code, name in sorted(cfg.STATE_NAMES.items(), key=lambda kv: kv[1]):
            self.cmb_state.addItem(f"{name} ({code})", code)
        self.cmb_scheme = QComboBox()
        for key in cfg.TAX_SCHEMES:
            self.cmb_scheme.addItem(tr(SCHEME_TEXT[key]), key)
        self.txt_gstin.setPlaceholderText(tr("15 characters, for example 27ABCDE1234F1Z5"))
        self.txt_upi.setPlaceholderText(tr("name@bank"))
        rows.addRow(tr("Shop name"), self.txt_name)
        rows.addRow(tr("Address"), self.txt_address)
        rows.addRow(tr("Phone"), self.txt_phone)
        rows.addRow(tr("Email"), self.txt_email)
        rows.addRow(tr("Tax type"), self.cmb_scheme)
        rows.addRow(tr("GSTIN"), self.txt_gstin)
        rows.addRow(tr("State (taken from the GSTIN when there is one)"), self.cmb_state)
        rows.addRow(tr("UPI id for the bill QR code"), self.txt_upi)
        box.setLayout(rows)
        col.addWidget(box)
        col.addWidget(label(tr("A change is saved as a new version. Old bills keep the details they were printed with."),
                            "subtitle"))
        self.btn_save_shop = QPushButton(tr("Save shop details"))
        self.btn_save_shop.clicked.connect(self.save_shop)
        col.addWidget(self.btn_save_shop)
        col.addStretch(1)
        for edit in (self.txt_name, self.txt_address, self.txt_phone, self.txt_email, self.txt_gstin, self.txt_upi):
            edit.textChanged.connect(self._changed)
        self.cmb_state.currentIndexChanged.connect(self._changed)
        self.cmb_scheme.currentIndexChanged.connect(self._changed)
        return page

    def _build_prefs_tab(self) -> QWidget:
        inner = QWidget()
        col = QVBoxLayout(inner)
        prefs = cfg.get_preferences(self.conn)  # only the layout (labels, kinds, choices) is read here
        groups = {}
        for key, p in prefs.items():
            groups.setdefault(p["group"], []).append((key, p))
        for group, items in groups.items():
            box = QGroupBox(tr(group))
            rows = form()
            for key, p in items:
                widget = self._make_pref_widget(key, p)
                self._pref_widgets[key] = (p["kind"], widget)
                rows.addRow(tr(p["label"]), widget)
            box.setLayout(rows)
            col.addWidget(box)
        col.addWidget(label(tr("Language and the idle-lock time apply the next time someone signs in."), "subtitle"))
        buttons = QHBoxLayout()
        self.btn_save_prefs = QPushButton(tr("Save preferences"))
        self.btn_undo = QPushButton(tr("Undo my changes"))
        self.btn_save_prefs.clicked.connect(self.save_preferences)
        self.btn_undo.clicked.connect(self.undo)
        buttons.addWidget(self.btn_save_prefs)
        buttons.addWidget(self.btn_undo)
        buttons.addStretch(1)
        col.addLayout(buttons)
        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _make_pref_widget(self, key, p):
        if p["kind"] == "bool":
            widget = QCheckBox()
            widget.toggled.connect(self._changed)
            return widget
        if p["kind"] == "choice":
            widget = QComboBox()
            for choice in p["choices"]:
                text = SUPPORTED.get(choice) if key.endswith("language") else tr(CHOICE_TEXT.get(choice, choice))
                widget.addItem(text, choice)
            widget.currentIndexChanged.connect(self._changed)
            return widget
        widget = QLineEdit()
        widget.textChanged.connect(self._changed)
        return widget

    def _build_tax_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.addWidget(label(
            tr("GST rates used on bills. The rate follows the HSN code and the price of one piece. "
               "Confirm every change with your accountant."), "subtitle"))
        self.tbl_tax = make_table([tr("HSN starts with"), tr("GST %"), tr("Price of one piece up to"),
                                   tr("Valid from"), tr("Valid until"), tr("Status"), tr("Note")])
        col.addWidget(self.tbl_tax, 1)
        col.addWidget(label(
            tr("A rate is never edited or deleted. To change a rate, end the old rule and add a new one that "
               "starts after it. Bills already made keep their own figures."), "subtitle"))
        buttons = QHBoxLayout()
        self.btn_add_rule = QPushButton(tr("Add a rate rule…"))
        self.btn_end_rule = QPushButton(tr("End the selected rule…"))
        self.btn_add_rule.clicked.connect(self.add_rule)
        self.btn_end_rule.clicked.connect(self.end_rule)
        buttons.addWidget(self.btn_add_rule)
        buttons.addWidget(self.btn_end_rule)
        buttons.addStretch(1)
        col.addLayout(buttons)
        return page

    def _build_series_tab(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.addWidget(label(
            tr("Bill and document numbers look like INV/26-27/000123: prefix, financial year, running number. "
               "The running number can never be changed or reused."), "subtitle"))
        self.tbl_series = make_table([tr("Document"), tr("Prefix"), tr("Financial year"), tr("Last number"),
                                      tr("Next number will be"), tr("Note")])
        col.addWidget(self.tbl_series, 1)
        self.btn_prefix = QPushButton(tr("Change the prefix of the selected document…"))
        self.btn_prefix.clicked.connect(self.change_prefix)
        col.addWidget(self.btn_prefix)
        return page

    # ------------------------------------------------------------ loading

    def refresh(self, *_):
        """Called each time the page is opened. What the owner is typing is never thrown away."""
        if self._dirty:
            return
        self.reload()

    def reload(self):
        ok, profile = self._run(lambda: cfg.get_shop_profile(self.conn), title=tr("Settings"), quiet_to=self.lbl_status)
        ok2, prefs = self._run(lambda: cfg.get_preferences(self.conn), title=tr("Settings"), quiet_to=self.lbl_status)
        ok3, rules = self._run(lambda: tax_admin.list_tax_rules(self.conn), title=tr("Settings"),
                               quiet_to=self.lbl_status)
        ok4, series = self._run(lambda: series_admin.list_series(self.conn), title=tr("Settings"),
                                quiet_to=self.lbl_status)
        if ok:
            self._profile = profile
            self._set_shop_widgets(profile)
        if ok2:
            self._set_pref_widgets(prefs)
        if ok3:
            self._fill_tax(rules)
        if ok4:
            self._fill_series(series)
        self._set_dirty(False)

    def _set_shop_widgets(self, p):
        for edit, key in ((self.txt_name, "name"), (self.txt_address, "address"), (self.txt_phone, "phone"),
                          (self.txt_email, "email"), (self.txt_gstin, "gstin"), (self.txt_upi, "upi_id")):
            edit.blockSignals(True)
            edit.setText(p.get(key) or "")
            edit.blockSignals(False)
        for combo, value in ((self.cmb_state, p.get("state_code")), (self.cmb_scheme, p.get("tax_scheme"))):
            combo.blockSignals(True)
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _set_pref_widgets(self, prefs):
        for key, (kind, widget) in self._pref_widgets.items():
            value = prefs[key]["value"]
            widget.blockSignals(True)
            if kind == "bool":
                widget.setChecked(bool(value))
            elif kind == "choice":
                index = widget.findData(value)
                widget.setCurrentIndex(index if index >= 0 else 0)
            else:
                widget.setText("" if value is None else str(value))
            widget.blockSignals(False)

    def _fill_tax(self, rules):
        self._rules = rules
        self.tbl_tax.setRowCount(0)
        for r in rules:
            ceiling = r.get("price_ceiling_paise")
            add_row(self.tbl_tax, [
                r["hsn_prefix"], f"{r['rate_percent']:g}",
                format_rupees(ceiling) if ceiling is not None else tr("No limit"),
                format_ui_date(r["valid_from"]), format_ui_date(r["valid_to"]) if r.get("valid_to") else "",
                tr(RULE_STATUS[r["status"]]), r.get("note") or "",
            ], right=(1, 2))

    def _fill_series(self, series):
        self._series = series
        self.tbl_series.setRowCount(0)
        for r in series:
            note = (tr("New prefix {prefix} starts next financial year.", prefix=r["prefix"])
                    if r["starts_next_year"] else "")
            add_row(self.tbl_series, [tr(r["label"]), r["prefix_in_use"], r["fin_year"], r["last_number"],
                                      r["next_number"], note], right=(3,))

    # ------------------------------------------------------------ typing and unsaved changes

    def _changed(self, *_):
        self._set_dirty(True)

    def _set_dirty(self, value: bool):
        self._dirty = value
        self.lbl_dirty.setText(tr("You have changes that are not saved yet.") if value else "")

    def undo(self, *_):
        self._dirty = False
        self.reload()

    # ------------------------------------------------------------ saving

    def _profile_fields(self) -> dict:
        fields = {
            "name": self.txt_name.text(), "address": self.txt_address.text(), "phone": self.txt_phone.text(),
            "email": self.txt_email.text(), "gstin": self.txt_gstin.text(), "tax_scheme": self.cmb_scheme.currentData(),
            "upi_id": self.txt_upi.text(),
        }
        if not self.txt_gstin.text().strip():  # with a GSTIN the state comes from it
            fields["state_code"] = self.cmb_state.currentData()
        return fields

    def save_shop(self, *_):
        if self._busy:
            return
        fields = self._profile_fields()
        old_scheme = self._profile.get("tax_scheme")
        if self._profile.get("is_set_up") and fields["tax_scheme"] != old_scheme:
            if not self._ask(tr("Settings"), tr(
                    "Changing the tax type changes which bills you can issue from now on. "
                    "Old bills stay as they are. Continue?")):
                return
        self._busy = True
        try:
            ok, changed = self._run(lambda: cfg.update_shop_profile(self.conn, self.user_id, **fields),
                                    title=tr("Settings"))
        finally:
            self._busy = False
        if not ok:
            return
        self.reload()
        self._info(tr("Settings"), tr("Shop details saved.") if changed else tr("Nothing was changed."))

    def _pref_values(self) -> dict:
        values = {}
        for key, (kind, widget) in self._pref_widgets.items():
            if kind == "bool":
                values[key] = widget.isChecked()
            elif kind == "choice":
                values[key] = widget.currentData()
            else:
                values[key] = widget.text()
        return values

    def save_preferences(self, *_):
        if self._busy:
            return
        values = self._pref_values()
        self._busy = True
        try:
            ok, changed = self._run(lambda: cfg.update_preferences(self.conn, self.user_id, values),
                                    title=tr("Settings"))
        finally:
            self._busy = False
        if not ok:
            return
        self.reload()
        if not changed:
            self._info(tr("Settings"), tr("Nothing was changed."))
        elif NEXT_SIGN_IN & set(changed):
            self._info(tr("Settings"), tr("Preferences saved. The language and idle-lock changes apply the next time "
                                          "someone signs in."))
        else:
            self._info(tr("Settings"), tr("Preferences saved."))

    # ------------------------------------------------------------ GST rate rules

    def _rule_dialog(self):
        """The details of a new rule as typed, or None when cancelled (tests replace this)."""
        dialog = RuleDialog(self)
        return dialog.values() if dialog.exec() == QDialog.Accepted else None

    def _text_dialog(self, title: str, text: str, default: str = ""):
        """One line of text, or None when cancelled (tests replace this)."""
        value, accepted = QInputDialog.getText(self, title, text, text=default)
        return value if accepted else None

    def add_rule(self, *_):
        if self._busy:
            return
        typed = self._rule_dialog()
        if typed is None:
            return
        self._busy = True
        try:
            ok, _ = self._run(lambda: tax_admin.add_tax_rule(
                self.conn, self.user_id, typed["hsn"], typed["rate"],
                parse_rupees(typed["limit"], allow_zero=False) if typed["limit"].strip() else None,
                parse_ui_date(typed["start"], "Start date"), typed["note"]), title=tr("Settings"))
        finally:
            self._busy = False
        if ok:
            self.reload()
            self._info(tr("Settings"), tr("GST rate rule added."))

    def _chosen_rule(self):
        row = self.tbl_tax.currentRow()
        return self._rules[row] if 0 <= row < len(self._rules) else None

    def end_rule(self, *_):
        if self._busy:
            return
        rule = self._chosen_rule()
        if rule is None:
            self._error(tr("Settings"), tr("Choose a rule in the table first."))
            return
        end = self._text_dialog(tr("End the rule"), tr("Last day this rule applies (DD/MM/YYYY)"),
                                date.today().strftime("%d/%m/%Y"))
        if end is None:
            return
        self._busy = True
        try:
            ok, _ = self._run(lambda: tax_admin.end_tax_rule(
                self.conn, self.user_id, rule["id"], parse_ui_date(end, "End date")), title=tr("Settings"))
        finally:
            self._busy = False
        if ok:
            self.reload()
            self._info(tr("Settings"), tr("The rule has been ended."))

    # ------------------------------------------------------------ document number prefix

    def change_prefix(self, *_):
        if self._busy:
            return
        row = self.tbl_series.currentRow()
        if not 0 <= row < len(self._series):
            self._error(tr("Settings"), tr("Choose a document in the table first."))
            return
        chosen = self._series[row]
        prefix = self._text_dialog(tr("Change the prefix"), tr("New prefix (1 to 8 letters or digits)"),
                                   chosen["prefix"])
        if prefix is None:
            return
        self._busy = True
        try:
            ok, result = self._run(lambda: series_admin.set_series_prefix(
                self.conn, self.user_id, chosen["series"], prefix), title=tr("Settings"))
        finally:
            self._busy = False
        if not ok:
            return
        self.reload()
        if not result["changed"]:
            self._info(tr("Settings"), tr("Nothing was changed."))
        elif result["applies_from"] == "now":
            self._info(tr("Settings"), tr("The prefix has been changed. It is used from the next number."))
        else:
            self._info(tr("Settings"), tr(
                "Documents were already made this year, so the new prefix starts with the next financial year "
                "({year}). Numbers already issued are not changed.", year=result["applies_from"]))


def build(conn, session, folder):
    return SettingsScreen(conn, session)
