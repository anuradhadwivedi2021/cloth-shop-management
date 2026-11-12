"""Fabric menu: fabric sold by the metre (PRD-11).

Put a roll or a remnant (cut-off) on the shelf, see the pieces that are left, and set the shortest length that will be
cut. Selling is done on the billing counter, which asks for the length in metres. No SQL and no rules here: they are
in core/fabric.py. To make a product fabric, set its unit to "metre" in Products and price it per metre in whole rupees.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core import fabric
from core.money import format_rupees
from ui.i18n import N_, tr
from ui.screen_helpers import ScreenBase, add_row, fill_combo, make_table
from ui.widgets import label

MENU_TEXT = "Fabric"
KINDS = [(N_("Roll"), "roll"), (N_("Remnant (cut-off)"), "remnant")]
KIND_TEXT = dict((key, text) for text, key in KINDS)


class FabricScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._products = []
        self.lbl_status = label("", "error")
        self.lbl_info = label("", "hint")
        self.cmb_product, self.cmb_variant, self.cmb_kind = QComboBox(), QComboBox(), QComboBox()
        fill_combo(self.cmb_kind, [(tr(text), key) for text, key in KINDS])
        self.txt_length, self.txt_ref, self.txt_min = QLineEdit(), QLineEdit(), QLineEdit()
        self.txt_length.setPlaceholderText(tr("Length in metres, for example 2.5"))
        self.txt_ref.setPlaceholderText(tr("Roll number or note (optional)"))
        self.txt_min.setPlaceholderText(tr("Shortest cut in metres (blank = none)"))
        self.btn_receive = QPushButton(tr("Add to stock"))
        self.btn_receive.setObjectName("primary")
        self.btn_min = QPushButton(tr("Save shortest cut"))
        self.tbl = make_table([tr("Piece"), tr("Colour"), tr("Reference"), tr("Length"), tr("Left")])

        top = QHBoxLayout()
        top.addWidget(self.cmb_product, 2)
        top.addWidget(self.cmb_variant, 1)
        add = QHBoxLayout()
        for w in (self.cmb_kind, self.txt_length, self.txt_ref, self.btn_receive):
            add.addWidget(w)
        cut = QHBoxLayout()
        cut.addWidget(self.txt_min, 1)
        cut.addWidget(self.btn_min)
        cut.addStretch(1)
        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(tr("Fabric"), "title"))
        col.addWidget(label(tr("Fabric is sold by the metre. To add a fabric, set the product's unit to metre in Products "
                               "and price it per metre in whole rupees."), "subtitle"))
        col.addWidget(self.lbl_status)
        col.addLayout(top)
        col.addWidget(self.lbl_info)
        col.addLayout(add)
        col.addLayout(cut)
        col.addWidget(label(tr("Rolls and remnants on the shelf"), "subtitle"))
        col.addWidget(self.tbl, 1)

        self.cmb_product.currentIndexChanged.connect(self._product_changed)
        self.cmb_variant.currentIndexChanged.connect(self._refresh)
        self.btn_receive.clicked.connect(self.receive)
        self.btn_min.clicked.connect(self.save_min)
        self.load()

    def load(self) -> None:
        ok, products = self._run(lambda: fabric.metre_products(self.conn, self.user_id), title=tr("Fabric"),
                                 quiet_to=self.lbl_status)
        self._products = products if ok else []
        keep = self.cmb_product.currentData()
        fill_combo(self.cmb_product, [(p["name"], p["id"]) for p in self._products], keep=keep)
        self._product_changed()

    def _product(self):
        pid = self.cmb_product.currentData()
        return next((p for p in self._products if p["id"] == pid), None)

    def _product_changed(self, *_) -> None:
        p = self._product()
        keep = self.cmb_variant.currentData()
        fill_combo(self.cmb_variant, [((f"{v['size']} {v['colour']}").strip(), v["id"]) for v in (p["variants"] if p else [])],
                   keep=keep)
        self.txt_min.setText(fabric.format_metres(p["min_cut_cm"]).removesuffix(" m") if p and p["min_cut_cm"] else "")
        self._refresh()

    def _refresh(self, *_) -> None:
        p, vid = self._product(), self.cmb_variant.currentData()
        self.tbl.setRowCount(0)
        if p is None or vid is None:
            self.lbl_info.setText(tr("No fabric yet."))
            return
        variant = next((v for v in p["variants"] if v["id"] == vid), None)
        self.lbl_info.setText(tr("In stock: {length}. Price: {price} per metre.",
                                 length=fabric.format_metres(variant["stock_cm"] if variant else 0),
                                 price=format_rupees(p["default_price_paise"])))
        ok, pieces = self._run(lambda: fabric.list_pieces(self.conn, self.user_id, vid), title=tr("Fabric"),
                               quiet_to=self.lbl_status)
        for piece in (pieces if ok else []):
            add_row(self.tbl, [tr(KIND_TEXT[piece["kind"]]), piece["colour"] or "", piece["ref"] or "",
                               fabric.format_metres(piece["length_cm"]), fabric.format_metres(piece["remaining_cm"])],
                    right={3, 4})

    def receive(self) -> None:
        vid = self.cmb_variant.currentData()
        if vid is None:
            self._error(tr("Fabric"), tr("Choose a fabric first."))
            return
        ok, _ = self._run(lambda: fabric.receive_piece(self.conn, self.user_id, vid, self.cmb_kind.currentData(),
                                                       self.txt_length.text(), self.txt_ref.text()), title=tr("Add to stock"))
        if ok:
            self.txt_length.clear()
            self.txt_ref.clear()
            self.load()

    def save_min(self) -> None:
        p = self._product()
        if p is None:
            return
        ok, _ = self._run(lambda: fabric.set_min_cut(self.conn, self.user_id, p["id"], self.txt_min.text()),
                          title=tr("Save shortest cut"))
        if ok:
            self.load()


def build(conn, session, folder):
    return FabricScreen(conn, session)
