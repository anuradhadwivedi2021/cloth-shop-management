"""Commission Rates menu: the shop's default rate and each salesperson's own rate (BIL-14).

No SQL and no rules here: core.commission checks and saves. The commission itself is a report (Reports >
Sales > Commission by salesperson), worked out from saved bills.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core import commission, shop_settings
from ui.i18n import tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Commission Rates"


class CommissionScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._people = []
        self.lbl_status = label("", "error")
        self.lbl_state = label("")

        self.default_edit = QLineEdit()
        self.default_edit.setMaximumWidth(120)
        self.btn_default = QPushButton(tr("Save default rate"))
        self.btn_toggle = QPushButton()
        self.person_edit = QLineEdit()
        self.person_edit.setMaximumWidth(120)
        self.person_edit.setPlaceholderText(tr("Own rate %"))
        self.btn_person = QPushButton(tr("Save rate for selected person"))
        self.btn_clear = QPushButton(tr("Use the default for selected person"))
        self.tbl = make_table([tr("Person"), tr("Own rate %"), tr("Rate used %")])

        top = QHBoxLayout()
        for w in (label(tr("Default rate %"), None), self.default_edit, self.btn_default, self.btn_toggle):
            top.addWidget(w)
        top.addStretch(1)
        mid = QHBoxLayout()
        for w in (self.person_edit, self.btn_person, self.btn_clear):
            mid.addWidget(w)
        mid.addStretch(1)

        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(tr("Commission Rates"), "title"))
        col.addWidget(label(tr("Commission is a percentage of net sales (before tax) of each salesperson's bills; "
                               "returns take it back. See Reports > Commission by salesperson."), "subtitle"))
        col.addWidget(self.lbl_state)
        col.addLayout(top)
        col.addLayout(mid)
        col.addWidget(self.lbl_status)
        col.addWidget(self.tbl, 1)

        self.btn_default.clicked.connect(self.save_default)
        self.btn_toggle.clicked.connect(self.toggle)
        self.btn_person.clicked.connect(self.save_person)
        self.btn_clear.clicked.connect(self.clear_person)
        self.load()

    def load(self) -> None:
        self.tbl.setRowCount(0)
        ok, people = self._run(lambda: commission.list_salespeople(self.conn, self.user_id), title=tr("Commission Rates"),
                               quiet_to=self.lbl_status)
        self._people = people if ok else []
        for p in self._people:
            own = "" if p["own_rate_bp"] is None else commission.percent_text(p["own_rate_bp"])
            add_row(self.tbl, [p["name"], own, commission.percent_text(p["rate_bp"])], right=(1, 2))
        self.default_edit.setText(commission.percent_text(commission.default_bp(self.conn)))
        on = commission.enabled(self.conn)
        self.lbl_state.setText(tr("Commission is ON: bills record the salesperson.") if on
                               else tr("Commission is OFF: nobody is credited on bills."))
        self.btn_toggle.setText(tr("Switch commission off") if on else tr("Switch commission on"))

    def _save_settings(self, changes) -> None:
        ok, _ = self._run(lambda: shop_settings.update_preferences(self.conn, self.user_id, changes), title=tr("Commission Rates"))
        if ok:
            self.load()

    def toggle(self) -> None:
        self._save_settings({"commission_enabled": "0" if commission.enabled(self.conn) else "1"})

    def save_default(self) -> None:
        def work():
            return commission.parse_percent(self.default_edit.text())
        ok, bp = self._run(work, title=tr("Save default rate"))
        if ok:
            self._save_settings({"commission_default_bp": str(bp)})

    def _selected(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            self._error(tr("Commission Rates"), tr("Select a person first."))
            return None
        return self._people[rows[0].row()]

    def save_person(self) -> None:
        person = self._selected()
        if person is None:
            return
        ok, _ = self._run(lambda: commission.set_rate(self.conn, self.user_id, person["id"], self.person_edit.text()),
                          title=tr("Save rate for selected person"))
        if ok:
            self.person_edit.clear()
            self.load()

    def clear_person(self) -> None:
        person = self._selected()
        if person is None:
            return
        ok, _ = self._run(lambda: commission.set_rate(self.conn, self.user_id, person["id"], None),
                          title=tr("Use the default for selected person"))
        if ok:
            self.load()


def build(conn, session, folder):
    return CommissionScreen(conn, session)
