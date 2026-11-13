"""Second Counter menu (main PC): share this shop with another PC on the same shop network.

Off unless the owner starts it. Never uses the internet: only addresses on the shop network are accepted. All rules
are in lan/. The second PC signs in with "Connect to the main counter" on its sign-in page and needs the pairing
code shown here.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from core.settings import get_setting, set_setting
from lan import service
from lan.protocol import DEFAULT_PORT
from ui.i18n import tr
from ui.screen_helpers import ScreenBase, fill_combo
from ui.widgets import label

MENU_TEXT = "Second Counter"


class LanScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, folder, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.folder, self.user_id = conn, session, folder, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.lbl_status = label("", "error")
        self.lbl_state = label("")
        self.lbl_code = label("", "title")
        self.lbl_help = label("", "hint")
        self.cmb_address = QComboBox()
        self.btn_find = QPushButton(tr("Show this PC's addresses"))
        self.btn_toggle = QPushButton()
        self.btn_toggle.setObjectName("primary")
        self.btn_new_code = QPushButton(tr("Make a new pairing code"))
        self.chk_auto = QCheckBox(tr("Start the second counter automatically when this app opens"))

        row = QHBoxLayout()
        row.addWidget(label(tr("This PC's address"), None))
        row.addWidget(self.cmb_address)
        row.addWidget(self.btn_find)
        row.addWidget(self.btn_toggle)
        row.addStretch(1)
        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(tr("Second Counter"), "title"))
        col.addWidget(label(tr("A second PC on the same shop WiFi or LAN can bill on this shop at the same time. "
                               "Nothing goes to the internet. Keep this app open on this PC while the second counter works."),
                            "subtitle"))
        col.addWidget(self.lbl_status)
        col.addWidget(self.lbl_state)
        col.addLayout(row)
        col.addWidget(label(tr("Pairing code (type it on the second PC):"), None))
        col.addWidget(self.lbl_code)
        col.addWidget(self.btn_new_code, 0, Qt.AlignLeft)
        col.addWidget(self.chk_auto)
        col.addWidget(self.lbl_help)
        col.addStretch(1)

        self.btn_toggle.clicked.connect(self.toggle)
        self.btn_new_code.clicked.connect(self.make_new_code)
        self.chk_auto.toggled.connect(self.save_auto)
        self.chk_auto.setChecked(get_setting(conn, "lan_autostart") == "1")
        self.btn_find.clicked.connect(self.find_addresses)
        fill_combo(self.cmb_address, [(tr("Detect automatically"), None)])     # the network is asked only on request
        self.refresh()

    def refresh(self) -> None:
        server = service.current()
        self.lbl_code.setText(service.pairing_code(self.conn))
        self.btn_new_code.setEnabled(server is None)
        self.cmb_address.setEnabled(server is None)
        self.btn_find.setEnabled(server is None)
        if server is None:
            self.lbl_state.setText(tr("The second counter is OFF."))
            self.btn_toggle.setText(tr("Start the second counter"))
            self.lbl_help.setText("")
        else:
            self.lbl_state.setText(tr("The second counter is ON: {address}", address=f"{server.host}:{server.port}"))
            self.btn_toggle.setText(tr("Stop the second counter"))
            self.lbl_help.setText(tr("On the second PC: sign-in page > Connect to the main counter > address {address}, "
                                     "then the pairing code above.", address=server.host))

    def find_addresses(self) -> None:
        found = service.local_addresses()
        fill_combo(self.cmb_address, [(tr("Detect automatically"), None)] + [(a, a) for a in found])
        if not found:
            self._error(tr("Second Counter"), tr("This PC is not on a shop network (no private address found)."))

    def toggle(self) -> None:
        if service.current() is not None:
            service.stop()
            self.refresh()
            return
        ok, _ = self._run(lambda: service.start(self.conn, self.session, self.folder, host=self.cmb_address.currentData(),
                                                port=DEFAULT_PORT), title=tr("Second Counter"))
        self.refresh()

    def make_new_code(self) -> None:
        self._run(lambda: service.new_code(self.conn, self.user_id), title=tr("Make a new pairing code"))
        self.refresh()

    def save_auto(self, checked) -> None:
        set_setting(self.conn, "lan_autostart", "1" if checked else "0")


def build(conn, session, folder):
    return LanScreen(conn, session, folder)
