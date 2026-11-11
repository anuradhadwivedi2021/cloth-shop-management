"""Pictures of one product (PRD-07), shown inside the product detail page. No SQL: core.product_images does the work."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from core import product_images as pi
from core.permissions import has_permission
from ui.i18n import tr
from ui.screen_helpers import ScreenBase
from ui.widgets import label


class ProductPhotosPanel(ScreenBase, QWidget):
    def __init__(self, conn, session, product_id, parent=None):
        super().__init__(parent)
        self.conn, self.session, self.user_id, self.product_id = conn, session, session.user_id, product_id
        self.status = label("", "error")
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setIconSize(QSize(96, 96))
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setFixedHeight(130)
        self.add_btn = QPushButton(tr("Add picture..."))
        self.remove_btn = QPushButton(tr("Remove selected picture"))
        can_edit = has_permission(conn, self.user_id, "edit_products")
        self.add_btn.setEnabled(can_edit)
        self.remove_btn.setEnabled(can_edit)
        self.add_btn.clicked.connect(self.add)
        self.remove_btn.clicked.connect(self.remove)
        bar = QHBoxLayout()
        bar.addWidget(self.add_btn)
        bar.addWidget(self.remove_btn)
        bar.addWidget(label(tr("PNG or JPEG, up to 2 MB each, up to 4 pictures."), "hint"), 1)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.addWidget(label(tr("Pictures"), "subtitle", wrap=False))
        col.addWidget(self.list)
        col.addLayout(bar)
        col.addWidget(self.status)
        self.load()

    def load(self) -> None:
        self.list.clear()
        ok, images = self._run(lambda: pi.list_images(self.conn, self.user_id, self.product_id),
                               title=tr("Pictures"), quiet_to=self.status)
        for img in (images if ok else []):
            got = pi.get_image(self.conn, self.user_id, img["id"])
            pixmap = QPixmap()
            pixmap.loadFromData(got["data"])
            item = QListWidgetItem(QIcon(pixmap), got["file_name"])
            item.setData(Qt.UserRole, img["id"])
            self.list.addItem(item)
        can_edit = has_permission(self.conn, self.user_id, "edit_products")
        self.add_btn.setEnabled(can_edit and self.list.count() < pi.MAX_PER_PRODUCT)
        self.remove_btn.setEnabled(can_edit)

    def add(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, tr("Add picture..."), "", "Pictures (*.png *.jpg *.jpeg)")
        if not path:
            return
        ok, _ = self._run(lambda: pi.add_image(self.conn, self.user_id, self.product_id, path), title=tr("Add picture..."))
        if ok:
            self.load()

    def remove(self) -> None:
        item = self.list.currentItem()
        if item is None:
            self._error(tr("Remove selected picture"), tr("Select a picture first."))
            return
        if not self._ask(tr("Remove selected picture"), tr("Remove this picture?")):
            return
        ok, _ = self._run(lambda: pi.remove_image(self.conn, self.user_id, item.data(Qt.UserRole)),
                          title=tr("Remove selected picture"))
        if ok:
            self.load()
