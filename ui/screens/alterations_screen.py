"""Alterations menu: tailoring / alteration job orders (ALT-01, ALT-02).

Take a garment in, move the job on (received > in progress > ready > delivered), and see who to call (ready for
pickup) and which jobs are late. A job register only: it is not a bill. All rules are in core/alterations.py.
"""

from datetime import date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget

from core import alterations as alt
from core.money import format_rupees, parse_rupees
from core.purchase_service import format_ui_date, parse_ui_date
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, fill_combo, make_table
from ui.widgets import label

MENU_TEXT = "Alterations"
STATUS_TEXT = {"received": N_("Received"), "in_progress": N_("In progress"), "ready": N_("Ready"),
               "delivered": N_("Delivered"), "cancelled": N_("Cancelled")}
FILTERS = [(N_("All"), "all"), (N_("Open"), "open"), (N_("Ready for pickup"), "ready"), (N_("Overdue"), "overdue"),
           (N_("Delivered"), "delivered"), (N_("Cancelled"), "cancelled")]
NEXT_STEP = {"received": ("in_progress", N_("Start work")), "in_progress": ("ready", N_("Mark ready")),
             "ready": ("delivered", N_("Deliver and take the balance"))}


class AlterationsScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._rows = []
        self.lbl_status = label("", "error")

        # ---- new job
        self.txt_name, self.txt_phone = QLineEdit(), QLineEdit()
        self.txt_name.setPlaceholderText(tr("Customer name"))
        self.txt_phone.setPlaceholderText(tr("Customer phone"))
        self.txt_item, self.txt_work, self.txt_measure = QLineEdit(), QLineEdit(), QLineEdit()
        self.txt_item.setPlaceholderText(tr("Item, for example Blue shirt"))
        self.txt_work.setPlaceholderText(tr("Work to do, for example Shorten sleeves"))
        self.txt_measure.setPlaceholderText(tr("Measurements (optional)"))
        self.txt_charge, self.txt_advance, self.txt_promised = QLineEdit(), QLineEdit(), QLineEdit()
        self.txt_charge.setPlaceholderText(tr("Charge (Rs)"))
        self.txt_advance.setPlaceholderText(tr("Advance taken (Rs)"))
        self.txt_promised.setPlaceholderText(tr("Promised date DD/MM/YYYY"))
        self.btn_save = QPushButton(tr("Save job"))
        self.btn_save.setObjectName("primary")
        new_page = QWidget()
        col = QVBoxLayout(new_page)
        col.addWidget(label(tr("A job order is not a bill. The advance and balance are written on the job only."), "hint"))
        for pair in ((self.txt_name, self.txt_phone), (self.txt_item, self.txt_work), (self.txt_measure,),
                     (self.txt_charge, self.txt_advance, self.txt_promised)):
            row = QHBoxLayout()
            for widget in pair:
                row.addWidget(widget, 1)
            col.addLayout(row)
        col.addWidget(self.btn_save)
        col.addStretch(1)

        # ---- the list
        self.cmb_filter = QComboBox()
        fill_combo(self.cmb_filter, [(tr(text), key) for text, key in FILTERS], keep=True)
        self.tbl = make_table([tr("Job no."), tr("Customer"), tr("Item"), tr("Work"), tr("Charge"), tr("Balance"),
                               tr("Promised"), tr("Status")])
        self.lbl_detail = label("", "hint")
        self.btn_next = QPushButton(tr("Next step"))
        self.btn_next.setObjectName("primary")
        self.btn_cancel = QPushButton(tr("Cancel this job"))
        list_page = QWidget()
        lcol = QVBoxLayout(list_page)
        lcol.addWidget(self.cmb_filter)
        lcol.addWidget(self.tbl, 1)
        lcol.addWidget(self.lbl_detail)
        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_next)
        lcol.addLayout(buttons)

        self.tabs = QTabWidget()
        self.tabs.addTab(new_page, tr("New job"))
        self.tabs.addTab(list_page, tr("Jobs"))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.addWidget(label(tr("Alterations"), "title"))
        outer.addWidget(self.lbl_status)
        outer.addWidget(self.tabs, 1)

        self.btn_save.clicked.connect(self.save_job)
        self.cmb_filter.currentIndexChanged.connect(self.reload)
        self.tbl.itemSelectionChanged.connect(self._show_selected)
        self.btn_next.clicked.connect(self.next_step)
        self.btn_cancel.clicked.connect(self.cancel_job)
        self.txt_promised.setText(format_ui_date((date.today() + timedelta(days=3)).isoformat()))
        self.reload()

    # ------------------------------------------------------------------ new job

    def save_job(self) -> None:
        title = tr("Save job")

        def work():
            charge = parse_rupees(self.txt_charge.text())
            advance = parse_rupees(self.txt_advance.text()) if self.txt_advance.text().strip() else 0
            return alt.create_job(
                self.conn, self.user_id, item=self.txt_item.text(), work=self.txt_work.text(), charge_paise=charge,
                advance_paise=advance, promised_date=parse_ui_date(self.txt_promised.text(), tr("The promised date")),
                measurements=self.txt_measure.text(), customer_name=self.txt_name.text(),
                customer_phone=self.txt_phone.text())
        ok, job_id = self._run(work, title=title)
        if not ok:
            return
        job = alt.get_job(self.conn, self.user_id, job_id)
        for edit in (self.txt_name, self.txt_phone, self.txt_item, self.txt_work, self.txt_measure, self.txt_charge,
                     self.txt_advance):
            edit.clear()
        self._info(title, tr("Job {no} saved. Balance to take on delivery: {amount}.", no=job["job_no"],
                             amount=format_rupees(job["balance_paise"])))
        self.reload()

    # ------------------------------------------------------------------ the list

    def _jobs(self):
        key = self.cmb_filter.currentData()
        if key == "ready":
            return alt.ready_for_pickup(self.conn, self.user_id)
        if key == "overdue":
            return alt.overdue(self.conn, self.user_id)
        jobs = alt.list_jobs(self.conn, self.user_id)
        if key == "open":
            return [j for j in jobs if j["status"] in alt.OPEN]
        return jobs if key in (None, "all") else [j for j in jobs if j["status"] == key]

    def reload(self, *_) -> None:
        keep = (self._selected() or {}).get("id")
        ok, jobs = self._run(self._jobs, title=tr("Alterations"), quiet_to=self.lbl_status)
        self._rows = jobs if ok else []
        today = date.today().isoformat()
        self.tbl.setRowCount(0)
        for j in self._rows:
            late = j["status"] in ("received", "in_progress") and j["promised_date"] < today
            status = tr(STATUS_TEXT[j["status"]]) + (" - " + tr("Late") if late else "")
            add_row(self.tbl, [j["job_no"], j["customer_name"] or "", j["item"], j["work"],
                               format_rupees(j["charge_paise"]), format_rupees(j["balance_paise"]),
                               format_ui_date(j["promised_date"]), status], right={4, 5})
        for row, j in enumerate(self._rows):
            if j["id"] == keep:
                self.tbl.selectRow(row)
        self._show_selected()

    def _selected(self):
        rows = self.tbl.selectionModel().selectedRows()
        return self._rows[rows[0].row()] if rows and rows[0].row() < len(self._rows) else None

    def _show_selected(self) -> None:
        job = self._selected()
        self.btn_cancel.setEnabled(bool(job and job["status"] in alt.OPEN))
        step = NEXT_STEP.get(job["status"]) if job else None
        self.btn_next.setEnabled(step is not None)
        self.btn_next.setText(tr(step[1]) if step else tr("Next step"))
        if job is None:
            self.lbl_detail.setText("")
            return
        bits = [job["customer_phone"] or "", job["measurements"] or ""]
        self.lbl_detail.setText("  |  ".join(b for b in bits if b))

    def next_step(self) -> None:
        job = self._selected()
        step = NEXT_STEP.get(job["status"]) if job else None
        if step is None:
            return
        status, text = step
        if status == "delivered" and not self._ask(
                tr("Alterations"), tr("Take {amount} from the customer and mark this job delivered?",
                                      amount=format_rupees(job["balance_paise"]))):
            return
        ok, _ = self._run(lambda: alt.set_status(self.conn, self.user_id, job["id"], status), title=tr(text))
        if ok:
            self.reload()

    def cancel_job(self) -> None:
        job = self._selected()
        if job is None or not self._ask(tr("Alterations"), tr("Cancel job {no}?", no=job["job_no"])):
            return
        ok, _ = self._run(lambda: alt.set_status(self.conn, self.user_id, job["id"], "cancelled"),
                          title=tr("Cancel this job"))
        if ok:
            self.reload()


def build(conn, session, folder):
    return AlterationsScreen(conn, session)
