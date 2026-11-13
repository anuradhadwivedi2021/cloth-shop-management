"""Import menu: bring products and variants in from a CSV or Excel file (PRD-10).

No SQL and no rules here. core.import_products reads and checks every row (preview), writes the error file,
and saves everything in ONE transaction. The Import button works only after a preview with no problems, and
the core checks the file again before saving. A message says "imported" only after the core call returned.
"""

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QGroupBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from core import import_products as ip
from ui.i18n import tr
from ui.screen_helpers import ScreenBase, add_row, make_table
from ui.widgets import label

MENU_TEXT = "Import Data"
log = logging.getLogger("clothshop")
SHOWN_PROBLEMS = 500          # the table shows the first ones; the error file has all of them


class ImportScreen(ScreenBase, QWidget):
    def __init__(self, conn, session, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id = conn, session, session.user_id
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._path = None
        self._preview = None
        self._busy = False

        self.lbl_status = label("", "error")
        self.lbl_file = label(tr("No file chosen."), "subtitle")
        self.lbl_summary = label("")

        intro = label(tr("Bring products in from a CSV or Excel (.xlsx) file. Each row is one size and colour of a "
                         "product. Nothing is saved until the check shows no problems."), "subtitle")
        self.btn_template = QPushButton(tr("Save an example file"))
        self.btn_choose = QPushButton(tr("Choose file and check it..."))
        self.btn_errors = QPushButton(tr("Save the problems as a file..."))
        self.btn_import = QPushButton(tr("Import now"))
        self.btn_errors.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.btn_template.clicked.connect(self.save_template)
        self.btn_choose.clicked.connect(self.choose_file)
        self.btn_errors.clicked.connect(self.save_errors)
        self.btn_import.clicked.connect(self.do_import)

        buttons = QHBoxLayout()
        for button in (self.btn_template, self.btn_choose, self.btn_errors, self.btn_import):
            buttons.addWidget(button)
        buttons.addStretch(1)

        self.tbl_problems = make_table([tr("Row"), tr("Column"), tr("Problem")])
        box = QGroupBox(tr("Problems found"))
        inner = QVBoxLayout(box)
        inner.addWidget(self.tbl_problems)

        col = QVBoxLayout(self)
        col.setContentsMargins(32, 28, 32, 28)
        col.addWidget(label(tr("Import Data"), "title"))
        col.addWidget(intro)
        col.addLayout(buttons)
        col.addWidget(self.lbl_file)
        col.addWidget(self.lbl_summary)
        col.addWidget(self.lbl_status)
        col.addWidget(box, 1)

    # ------------------------------------------------------------ actions

    def save_template(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, tr("Save an example file"), "products-example.csv", "CSV (*.csv)")
        if not path:
            return
        ok, saved = self._run(lambda: ip.write_template(path), title=tr("Example file"))
        if ok:
            self._info(tr("Example file"), tr("Saved: {path}", path=saved))

    def choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, tr("Choose file"), "", "CSV or Excel (*.csv *.xlsx)")
        if not path:
            return
        self._path = path
        self.lbl_file.setText(os.path.basename(path))
        self._check()

    def _check(self) -> None:
        self._preview = None
        self.btn_import.setEnabled(False)
        self.btn_errors.setEnabled(False)
        self.tbl_problems.setRowCount(0)
        self.lbl_summary.setText("")
        ok, preview = self._run(lambda: ip.preview_import(self.conn, self.user_id, self._path),
                                title=tr("Check the file"), quiet_to=self.lbl_status)
        if not ok:
            return
        self._preview = preview
        for problem in preview["problems"][:SHOWN_PROBLEMS]:
            add_row(self.tbl_problems, [problem["row"], problem["column"], tr(problem["message"])], right=(0,))
        self.btn_errors.setEnabled(bool(preview["problems"]))
        self.btn_import.setEnabled(preview["can_import"])
        if preview["problems"]:
            self.lbl_summary.setText(tr(
                "{bad} of {total} row(s) have problems. Nothing will be imported until they are fixed. "
                "Save the problems as a file, fix them in the file, and choose it again.",
                bad=preview["bad_rows"], total=preview["total_rows"]))
        else:
            self.lbl_summary.setText(tr(
                "The file is fine: {products} new product(s), {existing} existing product(s) getting new sizes, "
                "{variants} variant(s), {pieces} piece(s) of opening stock. Press Import now.",
                products=preview["new_products"], existing=preview["added_to_existing"],
                variants=preview["new_variants"], pieces=preview["opening_pieces"]))

    def save_errors(self) -> None:
        if not self._preview or not self._preview["problems"]:
            return
        path, _filter = QFileDialog.getSaveFileName(self, tr("Save the problems as a file..."), "import-problems.csv", "CSV (*.csv)")
        if not path:
            return
        ok, saved = self._run(lambda: ip.write_error_file(self._preview, path), title=tr("Problems file"))
        if ok:
            self._info(tr("Problems file"), tr("Saved: {path}", path=saved))

    def do_import(self) -> None:
        if self._busy or not self._preview or not self._preview["can_import"]:
            return
        if not self._ask(tr("Import now"), tr("Import {n} variant(s) from this file?", n=self._preview["new_variants"])):
            return
        self._busy = True
        try:
            ok, result = self._run(lambda: ip.import_products(self.conn, self.user_id, self._path), title=tr("Import now"))
        finally:
            self._busy = False
        if not ok:
            self._check()           # the file or the shop may have changed since the check
            return
        self.btn_import.setEnabled(False)
        self._preview = None
        message = tr("Imported {variants} variant(s) in {products} new product(s), with {pieces} piece(s) of opening stock.",
                     variants=result["variants"], products=result["products"], pieces=result["opening_pieces"])
        self.lbl_summary.setText(message)
        self._info(tr("Import now"), message)


def build(conn, session, folder):
    return ImportScreen(conn, session)
